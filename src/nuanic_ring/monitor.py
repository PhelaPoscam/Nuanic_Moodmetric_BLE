"""Real-time multi-ring monitor for Nuanic ring streams."""

import asyncio
import csv
import json
import math
import struct
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from .connector import NuanicConnector
from .mm_compat import MMFeatures, MMLikeScorer
from .signal_processing import SignalConditioner


def convert_eda(raw_value: int) -> Tuple[float, float]:
    """Convert raw EDA integer into resistance (kOhm) and conductance (uS)."""
    adc_multiplier = 1.0
    resistance_kohm = (raw_value * adc_multiplier) / 1000.0
    conductance_us = (1000.0 / resistance_kohm) if resistance_kohm > 0 else 0.0
    return round(resistance_kohm, 4), round(conductance_us, 4)


@dataclass
class RingDeviceState:
    """Per-device runtime state to keep data pipelines isolated."""

    mac: str
    calibration_seconds: int
    status: str = "disconnected"
    battery: Optional[int] = None

    # Latest values shown in dashboard
    raw_eda: Optional[int] = None
    filtered_us: Optional[float] = None
    arousal_score: float = 0.0
    imu_xyz: Tuple[Optional[int], Optional[int], Optional[int]] = (
        None,
        None,
        None,
    )
    dne_stress_index: Optional[int] = None

    # Counters and buffers
    d306_count: int = 0
    imu_batch_count: int = 0
    state_count: int = 0
    live_eda_count: int = 0
    d306_buffer: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=10)
    )
    imu_batch_buffer: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=5)
    )

    # Independent processing chain per ring
    signal_conditioner: SignalConditioner = field(
        default_factory=SignalConditioner
    )
    scorer: MMLikeScorer = field(init=False)

    # Logging
    log_file: Optional[Path] = None
    log_queue: Optional[asyncio.Queue[List[Any]]] = None
    writer_task: Optional[asyncio.Task[None]] = None
    dropped_rows: int = 0

    # Reconnect bookkeeping
    reconnect_attempt: int = 0
    last_seen: Optional[datetime] = None

    # Rate diagnostics and control status
    d306_observed_hz: float = 0.0
    imu_observed_hz: float = 0.0
    last_d306_ts: Optional[datetime] = None
    last_imu_ts: Optional[datetime] = None
    d306_intervals: Deque[float] = field(
        default_factory=lambda: deque(maxlen=128)
    )
    imu_intervals: Deque[float] = field(
        default_factory=lambda: deque(maxlen=128)
    )
    d306_would_drop: int = 0
    imu_would_drop: int = 0
    rate_control_status: str = "not-attempted"
    rate_control_detail: str = ""

    def __post_init__(self) -> None:
        self.scorer = MMLikeScorer(
            calibration_seconds=self.calibration_seconds
        )


class NuanicMonitor:
    """Multi-device monitor with isolated per-device state and logging."""

    def __init__(
        self,
        log_dir: str = "data/ring_logs",
        imu_refresh_packets: int = 5,
        clear_console: bool = True,
        enable_logging: bool = True,
        calibration_seconds: int = 60,
        target_hz: Optional[float] = None,
        equalize_mode: str = "off",
        attempt_ring_rate_control: bool = False,
    ):
        self.log_dir = Path(log_dir)
        self.enable_logging = enable_logging
        if self.enable_logging:
            self.log_dir.mkdir(parents=True, exist_ok=True)

        self.connector = NuanicConnector()
        self.imu_refresh_packets = max(1, imu_refresh_packets)
        self.clear_console = clear_console
        self.calibration_seconds = calibration_seconds
        self.target_hz = float(target_hz) if target_hz else None
        self.equalize_mode = equalize_mode
        self.attempt_ring_rate_control = attempt_ring_rate_control

        self.start_time: Optional[datetime] = None
        self.running = False
        self.capture_armed = False
        self.device_states: Dict[str, RingDeviceState] = {}

        self._health_task: Optional[asyncio.Task[None]] = None
        self._auto_reconnect = True
        self._reconnect_backoff_seconds = 2.0

    def _elapsed_seconds(self) -> float:
        if not self.start_time:
            return 0.0
        return max(0.001, (datetime.now() - self.start_time).total_seconds())

    def _parse_d306_packet(self, data: bytes) -> Optional[Dict[str, int]]:
        if len(data) != 16:
            return None

        return {
            "clock": struct.unpack("<I", data[0:4])[0],
            "context": struct.unpack("<I", data[4:8])[0],
            "eda_value": struct.unpack("<I", data[8:12])[0],
            "dne_stress_index": struct.unpack("<I", data[12:16])[0],
        }

    def _parse_468f_imu_batch(self, data: bytes) -> Optional[Dict[str, Any]]:
        if len(data) != 92:
            return None

        clock = struct.unpack("<I", data[0:4])[0]
        context = struct.unpack("<I", data[4:8])[0]

        samples: List[Tuple[int, int, int]] = []
        offset = 8
        for _ in range(14):
            x, y, z = struct.unpack_from("<hhh", data, offset)
            samples.append((x, y, z))
            offset += 6

        magnitudes = [
            math.sqrt((x * x) + (y * y) + (z * z))
            for x, y, z in samples
        ]
        motion_intensity = sum(magnitudes) / len(magnitudes)

        return {
            "clock": clock,
            "context": context,
            "samples": samples,
            "first_x": samples[0][0],
            "first_y": samples[0][1],
            "first_z": samples[0][2],
            "motion_intensity": motion_intensity,
        }

    def _ensure_device_state(self, mac: str) -> RingDeviceState:
        mac_key = mac.upper()
        state = self.device_states.get(mac_key)
        if state:
            return state

        state = RingDeviceState(
            mac=mac_key,
            calibration_seconds=self.calibration_seconds,
        )
        self.device_states[mac_key] = state

        if self.enable_logging:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            safe_mac = mac_key.replace(":", "-")
            state.log_file = self.log_dir / f"ring_{safe_mac}_{timestamp}.csv"
            with open(
                state.log_file,
                "w",
                newline="",
                encoding="utf-8",
            ) as file:
                writer = csv.writer(file)
                writer.writerow(
                    [
                        "timestamp",
                        "elapsed_ms",
                        "device_mac",
                        "connection_state",
                        "data_type",
                        "EDA_Raw_Value",
                        "Stress_Index",
                        "MM_Filtered_uS",
                        "MM_Arousal_Score",
                        "MM_Calibrated",
                        "Skin_Resistance_kOhm",
                        "Skin_Conductance_uS",
                        "D306_Clock",
                        "D306_Context",
                        "IMU_Batch_Clock",
                        "IMU_Batch_Context",
                        "IMU_X0",
                        "IMU_Y0",
                        "IMU_Z0",
                        "IMU_Motion_Intensity",
                        "State_Code",
                        "payload_hex",
                        "full_packet_hex",
                        "decoded_fields",
                        "D306_Observed_Hz",
                        "IMU_Observed_Hz",
                        "Rate_Target_Hz",
                        "Rate_Control_Status",
                        "Equalize_Mode",
                        "Equalize_WouldDrop",
                    ]
                )

            state.log_queue = asyncio.Queue(maxsize=5000)
            state.writer_task = asyncio.create_task(
                self._csv_writer_loop(state)
            )

        return state

    async def _csv_writer_loop(self, state: RingDeviceState) -> None:
        if not state.log_file or not state.log_queue:
            return

        batch: List[List[Any]] = []
        while self.running or not state.log_queue.empty():
            try:
                row = await asyncio.wait_for(
                    state.log_queue.get(),
                    timeout=0.2,
                )
                batch.append(row)
                if len(batch) < 64:
                    continue
            except asyncio.TimeoutError:
                pass

            if not batch:
                continue

            with open(
                state.log_file,
                "a",
                newline="",
                encoding="utf-8",
            ) as file:
                writer = csv.writer(file)
                writer.writerows(batch)
            batch.clear()

    def _enqueue_log(self, state: RingDeviceState, row: List[Any]) -> None:
        if not self.enable_logging or not state.log_queue:
            return

        try:
            state.log_queue.put_nowait(row)
        except asyncio.QueueFull:
            state.dropped_rows += 1

    def _base_row(self, state: RingDeviceState, data_type: str) -> List[Any]:
        timestamp = datetime.now().isoformat(timespec="milliseconds")
        elapsed_ms = int(self._elapsed_seconds() * 1000)
        return [
            timestamp,
            elapsed_ms,
            state.mac,
            state.status,
            data_type,
        ]

    def _update_observed_hz(
        self,
        state: RingDeviceState,
        stream_name: str,
        now: datetime,
    ) -> None:
        if stream_name == "d306":
            last = state.last_d306_ts
            if last is not None:
                dt = (now - last).total_seconds()
                if dt > 0:
                    state.d306_intervals.append(dt)
                    mean_dt = sum(state.d306_intervals) / len(
                        state.d306_intervals
                    )
                    if mean_dt > 0:
                        state.d306_observed_hz = 1.0 / mean_dt
            state.last_d306_ts = now
            return

        last = state.last_imu_ts
        if last is not None:
            dt = (now - last).total_seconds()
            if dt > 0:
                state.imu_intervals.append(dt)
                mean_dt = sum(state.imu_intervals) / len(state.imu_intervals)
                if mean_dt > 0:
                    state.imu_observed_hz = 1.0 / mean_dt
        state.last_imu_ts = now

    def _equalize_decision(
        self,
        state: RingDeviceState,
        stream_name: str,
    ) -> bool:
        if self.equalize_mode == "off" or not self.target_hz:
            return False

        target_dt = 1.0 / max(1e-6, self.target_hz)
        intervals = (
            state.d306_intervals
            if stream_name == "d306"
            else state.imu_intervals
        )
        if not intervals:
            return False

        latest_dt = intervals[-1]
        should_drop = latest_dt < target_dt
        if should_drop:
            if stream_name == "d306":
                state.d306_would_drop += 1
            else:
                state.imu_would_drop += 1
        return should_drop

    def _row_rate_tail(
        self,
        state: RingDeviceState,
        would_drop: bool,
    ) -> List[Any]:
        return [
            (
                f"{state.d306_observed_hz:.3f}"
                if state.d306_observed_hz > 0
                else ""
            ),
            (
                f"{state.imu_observed_hz:.3f}"
                if state.imu_observed_hz > 0
                else ""
            ),
            f"{self.target_hz:.2f}" if self.target_hz else "",
            state.rate_control_status,
            self.equalize_mode,
            "1" if would_drop else "0",
        ]

    def _make_imu_callback(self, mac: str):
        def _cb(_sender: Any, data: bytes) -> None:
            if not self.capture_armed:
                return

            state = self._ensure_device_state(mac)
            parsed = self._parse_d306_packet(data)
            if not parsed:
                return

            now = datetime.now()
            state.last_seen = now
            self._update_observed_hz(state, "d306", now)
            would_drop = self._equalize_decision(state, "d306")
            state.d306_count += 1

            clock = parsed["clock"]
            context = parsed["context"]
            eda_value = parsed["eda_value"]
            dne_stress_index = parsed["dne_stress_index"]

            resistance_kohm, conductance_us = convert_eda(eda_value)
            filtered_us = state.signal_conditioner.process(conductance_us)
            freq, amp = state.scorer.update_scr_features(
                tonic_value=filtered_us
            )
            score_state = state.scorer.update(
                MMFeatures(
                    scr_frequency_per_min=freq,
                    scr_amplitude=amp,
                    scl_microsiemens=filtered_us,
                )
            )

            state.raw_eda = eda_value
            state.filtered_us = filtered_us
            state.arousal_score = score_state["mm_like_1_to_100"]
            state.dne_stress_index = dne_stress_index
            state.d306_buffer.append(
                {
                    "clock": clock,
                    "context": context,
                    "eda_value": eda_value,
                    "dne_stress_index": dne_stress_index,
                }
            )

            row = self._base_row(state, "D306_EDA") + [
                eda_value,
                dne_stress_index,
                f"{filtered_us:.4f}",
                f"{state.arousal_score:.2f}",
                "1" if score_state["calibrated"] else "0",
                f"{resistance_kohm:.4f}",
                f"{conductance_us:.4f}",
                clock,
                context,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                data.hex(),
                "",
                "",
            ] + self._row_rate_tail(state, would_drop)
            self._enqueue_log(state, row)

        return _cb

    def _make_stress_callback(self, mac: str):
        def _cb(_sender: Any, data: bytes) -> None:
            if not self.capture_armed:
                return

            state = self._ensure_device_state(mac)
            parsed_batch = self._parse_468f_imu_batch(data)
            if not parsed_batch:
                return

            now = datetime.now()
            state.last_seen = now
            self._update_observed_hz(state, "imu", now)
            would_drop = self._equalize_decision(state, "imu")
            state.imu_batch_count += 1
            state.imu_xyz = (
                parsed_batch["first_x"],
                parsed_batch["first_y"],
                parsed_batch["first_z"],
            )
            state.imu_batch_buffer.append(parsed_batch)

            row = self._base_row(state, "IMU_BATCH_468F") + [
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                parsed_batch["clock"],
                parsed_batch["context"],
                parsed_batch["first_x"],
                parsed_batch["first_y"],
                parsed_batch["first_z"],
                f"{parsed_batch['motion_intensity']:.2f}",
                "",
                data[8:].hex(),
                data.hex(),
                "",
            ] + self._row_rate_tail(state, would_drop)
            self._enqueue_log(state, row)

        return _cb

    def _make_raw_eda_callback(self, mac: str):
        def _cb(_sender: Any, data: bytes) -> None:
            if not self.capture_armed:
                return

            state = self._ensure_device_state(mac)
            state.last_seen = datetime.now()
            state.state_count += 1
            state_code = data[0] if len(data) >= 1 else None
            would_drop = False

            row = self._base_row(state, "STATE_3C18") + [
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                state_code if state_code is not None else "",
                data.hex(),
                data.hex(),
                "",
            ] + self._row_rate_tail(state, would_drop)
            self._enqueue_log(state, row)

        return _cb

    def _make_live_eda_callback(self, mac: str):
        def _cb(_sender: Any, data: bytes) -> None:
            if not self.capture_armed:
                return

            state = self._ensure_device_state(mac)
            state.last_seen = datetime.now()
            state.live_eda_count += 1
            would_drop = False

            row = self._base_row(state, "LIVE_EDA_42DC") + [
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                data.hex(),
                data.hex(),
                json.dumps({"len": len(data)}),
            ] + self._row_rate_tail(state, would_drop)
            self._enqueue_log(state, row)

        return _cb

    async def _subscribe_device_streams(self, mac: str) -> bool:
        imu_ok = await self.connector.subscribe_to_imu(
            self._make_imu_callback(mac),
            address=mac,
        )
        stress_ok = await self.connector.subscribe_to_stress(
            self._make_stress_callback(mac),
            address=mac,
        )
        raw_ok = await self.connector.subscribe_to_raw_eda(
            self._make_raw_eda_callback(mac),
            address=mac,
        )
        live_ok = await self.connector.subscribe_to_live_eda(
            self._make_live_eda_callback(mac),
            address=mac,
        )
        return imu_ok and stress_ok and raw_ok and live_ok

    async def _unsubscribe_device_streams(self, mac: str) -> None:
        await self.connector.unsubscribe_from_imu(address=mac)
        await self.connector.unsubscribe_from_stress(address=mac)
        await self.connector.unsubscribe_from_raw_eda(address=mac)
        await self.connector.unsubscribe_from_live_eda(address=mac)

    async def _connect_and_subscribe(
        self,
        mac: str,
        device: Any = None,
    ) -> bool:
        state = self._ensure_device_state(mac)
        state.status = "connecting"

        ok = await self.connector.connect_device(address=mac, device=device)
        if not ok:
            state.status = "disconnected"
            return False

        state.status = "connected"
        state.reconnect_attempt = 0
        state.battery = await self.connector.read_battery(address=mac)

        if self.attempt_ring_rate_control and self.target_hz:
            result = await self.connector.attempt_set_sample_rate(
                target_hz=int(self.target_hz),
                address=mac,
            )
            state.rate_control_status = str(result.get("status", "unknown"))
            detail_bits = []
            if result.get("uuid"):
                detail_bits.append(str(result["uuid"])[0:8])
            if result.get("payload_hex"):
                detail_bits.append(f"p={result['payload_hex']}")
            if result.get("echo_hex"):
                detail_bits.append(f"e={result['echo_hex']}")
            state.rate_control_detail = " ".join(detail_bits)
        elif self.target_hz:
            state.rate_control_status = "not-requested"
        else:
            state.rate_control_status = "not-configured"

        streams_ok = await self._subscribe_device_streams(mac)
        if not streams_ok:
            state.status = "degraded"
            return False

        return True

    async def start_multi(
        self,
        ring_addresses: Optional[List[str]] = None,
        monitor_all: bool = False,
        max_devices: Optional[int] = None,
        stagger_delay: float = 1.25,
        auto_reconnect: bool = True,
    ) -> bool:
        """Start monitoring one or many rings.

                - If monitor_all=True and ring_addresses is empty,
                    discover all rings.
                - If ring_addresses is empty and monitor_all=False,
                    use interactive selection.
        """
        self.start_time = datetime.now()
        self.running = True
        self.capture_armed = False
        self._auto_reconnect = auto_reconnect

        if not ring_addresses and not monitor_all:
            # Backward-compatible single selection flow.
            if not await self.connector.connect():
                self.running = False
                return False
            client = self.connector.client
            if not client:
                self.running = False
                return False
            mac = client.address
            state = self._ensure_device_state(mac)
            state.status = "connected"
            state.battery = await self.connector.read_battery()
            ok = await self._subscribe_device_streams(mac)
            if not ok:
                state.status = "degraded"

            self.start_time = datetime.now()
            self.capture_armed = True

            self._health_task = asyncio.create_task(
                self._connection_health_loop()
            )
            return True

        # Multi-device path.
        discovered: List[Dict[str, Any]] = (
            await self.connector.discover_all_matching_rings(
                include_device=True,
                scan_timeout=6.0,
                attempts=3,
                retry_delay=0.5,
            )
        )
        discovered_by_mac = {d["address"].upper(): d for d in discovered}

        targets = [a.upper() for a in (ring_addresses or [])]
        if monitor_all and not targets:
            targets = list(discovered_by_mac.keys())
        if max_devices is not None:
            targets = targets[: max(0, max_devices)]

        connected_any = False
        for idx, mac in enumerate(targets):
            entry = discovered_by_mac.get(mac)
            ok = await self._connect_and_subscribe(
                mac=mac,
                device=(entry or {}).get("device"),
            )
            connected_any = connected_any or ok
            if idx < len(targets) - 1 and stagger_delay > 0:
                await asyncio.sleep(stagger_delay)

        self.start_time = datetime.now()
        self.capture_armed = True
        self._health_task = asyncio.create_task(self._connection_health_loop())
        return connected_any

    async def _connection_health_loop(self) -> None:
        while self.running:
            for mac, state in list(self.device_states.items()):
                client = self.connector.get_client(mac)
                is_connected = bool(
                    client and getattr(client, "is_connected", False)
                )

                if is_connected:
                    if state.status != "connected":
                        state.status = "connected"
                    continue

                if state.status == "connecting":
                    continue

                if not self._auto_reconnect:
                    state.status = "offline"
                    continue

                state.status = "reconnecting"
                state.reconnect_attempt += 1
                wait_seconds = min(
                    30.0,
                    self._reconnect_backoff_seconds
                    * (2 ** (state.reconnect_attempt - 1)),
                )
                await asyncio.sleep(wait_seconds)
                await self._unsubscribe_device_streams(mac)
                await self.connector.disconnect(address=mac)
                await self._connect_and_subscribe(mac)

            await asyncio.sleep(1.0)

    async def stop_multi(self) -> None:
        self.running = False
        self.capture_armed = False

        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        for mac in list(self.device_states.keys()):
            await self._unsubscribe_device_streams(mac)

        await self.connector.disconnect()

        for state in self.device_states.values():
            if state.writer_task:
                try:
                    await state.writer_task
                except asyncio.CancelledError:
                    pass

    def dashboard_rows(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        for mac in sorted(self.device_states.keys()):
            state = self.device_states[mac]
            imu_x, imu_y, imu_z = state.imu_xyz
            rows.append(
                {
                    "device_mac": mac,
                    "connection_status": state.status,
                    "battery": (
                        "N/A" if state.battery is None else f"{state.battery}%"
                    ),
                    "raw_eda": (
                        "N/A" if state.raw_eda is None else str(state.raw_eda)
                    ),
                    "filtered_us": (
                        "N/A"
                        if state.filtered_us is None
                        else f"{state.filtered_us:.3f}"
                    ),
                    "arousal_score": f"{state.arousal_score:.1f}",
                    "dne_score": (
                        "N/A"
                        if state.dne_stress_index is None
                        else f"{state.dne_stress_index}"
                    ),
                    "observed_hz": (
                        f"{state.d306_observed_hz:.1f}"
                        f"/{state.imu_observed_hz:.1f}"
                    ),
                    "rate_control": state.rate_control_status,
                    "imu_xyz": f"({imu_x}, {imu_y}, {imu_z})",
                }
            )
        return rows

    # Backward-compatible wrappers
    async def start_monitoring(self) -> bool:
        return await self.start_multi(monitor_all=False)

    async def stop_monitoring(self) -> None:
        await self.stop_multi()

    def get_current_stress(self) -> Optional[int]:
        if not self.device_states:
            return None
        first = next(iter(self.device_states.values()))
        return first.dne_stress_index

    def get_current_eda(self) -> Optional[int]:
        if not self.device_states:
            return None
        first = next(iter(self.device_states.values()))
        return first.raw_eda

    async def run(self, duration_seconds: Optional[float] = None) -> bool:
        ok = await self.start_multi(monitor_all=False)
        if not ok:
            return False

        try:
            if duration_seconds is None:
                while True:
                    await asyncio.sleep(1)
            else:
                await asyncio.sleep(duration_seconds)
            return True
        except (KeyboardInterrupt, asyncio.CancelledError):
            return True
        finally:
            await self.stop_multi()

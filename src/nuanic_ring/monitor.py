"""Real-time multi-ring monitor for Nuanic ring streams."""

import asyncio
import csv
import json
import math
import platform
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
    d306_buffer: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=10))
    imu_batch_buffer: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=5)
    )

    # Independent processing chain per ring
    signal_conditioner: SignalConditioner = field(default_factory=SignalConditioner)
    scorer: MMLikeScorer = field(init=False)

    # Logging
    log_file: Optional[Path] = None
    log_queue: Optional[asyncio.Queue[List[Any]]] = None
    writer_task: Optional[asyncio.Task[None]] = None
    stream_log_file: Optional[Path] = None
    computed_log_file: Optional[Path] = None
    stream_log_queue: Optional[asyncio.Queue[List[Any]]] = None
    computed_log_queue: Optional[asyncio.Queue[List[Any]]] = None
    stream_writer_task: Optional[asyncio.Task[None]] = None
    computed_writer_task: Optional[asyncio.Task[None]] = None
    dropped_rows: int = 0
    marker_count: int = 0

    # Reconnect bookkeeping
    reconnect_attempt: int = 0
    last_seen: Optional[datetime] = None

    # Rate diagnostics and control status
    d306_observed_hz: float = 0.0
    imu_observed_hz: float = 0.0
    last_d306_ts: Optional[datetime] = None
    last_imu_ts: Optional[datetime] = None
    last_accepted_d306_ts: Optional[datetime] = None
    last_accepted_imu_ts: Optional[datetime] = None
    d306_intervals: Deque[float] = field(default_factory=lambda: deque(maxlen=128))
    imu_intervals: Deque[float] = field(default_factory=lambda: deque(maxlen=128))
    d306_would_drop: int = 0
    imu_would_drop: int = 0
    rate_control_status: str = "not-attempted"
    rate_control_detail: str = ""
    heartbeat_tick: bool = False

    def __post_init__(self) -> None:
        self.scorer = MMLikeScorer(calibration_seconds=self.calibration_seconds)


class NuanicMonitor:
    """Multi-device monitor with isolated per-device state and logging."""

    def __init__(
        self,
        log_dir: str = "data/ring_logs",
        imu_refresh_packets: int = 5,
        clear_console: bool = True,
        enable_logging: bool = True,
        csv_layout: str = "combined",
        calibration_seconds: int = 60,
        target_hz: Optional[float] = None,
        equalize_mode: str = "off",
        attempt_ring_rate_control: bool = False,
        force_hz: bool = False,
        use_warmup: bool = False,
        warmup_delay: float = 3.0,
        allow_reset_bt: bool = False,
        participant_id: Optional[str] = None,
    ):
        self.log_dir = Path(log_dir)
        self.enable_logging = enable_logging
        if csv_layout not in {"combined", "split", "both"}:
            raise ValueError("csv_layout must be one of: combined, split, both")
        self.csv_layout = csv_layout
        self.force_hz = force_hz
        self.participant_id = participant_id
        if self.enable_logging:
            self.log_dir.mkdir(parents=True, exist_ok=True)

        self.connector = NuanicConnector()
        self.imu_refresh_packets = max(1, imu_refresh_packets)
        self.clear_console = clear_console
        self.calibration_seconds = calibration_seconds
        self.target_hz = target_hz
        self.equalize_mode = equalize_mode
        self.attempt_ring_rate_control = attempt_ring_rate_control
        self.use_warmup = use_warmup
        self.warmup_delay = warmup_delay
        self.allow_reset_bt = allow_reset_bt

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

        magnitudes = [math.sqrt((x * x) + (y * y) + (z * z)) for x, y, z in samples]
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
            if self.csv_layout in {"combined", "both"}:
                state.log_queue = asyncio.Queue(maxsize=5000)
            if self.csv_layout in {"split", "both"}:
                state.stream_log_queue = asyncio.Queue(maxsize=5000)
                state.computed_log_queue = asyncio.Queue(maxsize=5000)
            # writer_task will be created lazily in _initialize_log_file

        return state

    def _log_filename(self, state: RingDeviceState, suffix: str = "") -> str:
        timestamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
        safe_mac = state.mac.replace(":", "-")
        parts = ["SessionDate", timestamp]
        if self.participant_id:
            parts.append(self.participant_id)
        parts.append(f"ring-{safe_mac[-6:]}")
        if suffix:
            parts.append(suffix)
        return "_".join(parts) + ".csv"

    def _initialize_log_file(self, state: RingDeviceState) -> None:
        """Lazily initialize the CSV log file only when data starts arriving."""
        if not self.enable_logging or state.log_file:
            return

        filename = self._log_filename(state)
        state.log_file = self.log_dir / filename

        try:
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
            print(f"[LOG] Started lazy log for {state.mac}: {state.log_file.name}")
            if state.log_queue:
                state.writer_task = asyncio.create_task(
                    self._csv_writer_loop(state, state.log_queue, state.log_file)
                )
        except Exception as e:
            print(f"[LOG] Error initializing log for {state.mac}: {e}")
            state.log_file = None

    def _initialize_split_log_files(self, state: RingDeviceState) -> None:
        """Lazily initialize raw-stream and computed CSV files."""
        if not self.enable_logging or (
            state.stream_log_file and state.computed_log_file
        ):
            return

        stream_header = [
            "timestamp",
            "elapsed_ms",
            "device_mac",
            "connection_state",
            "data_type",
            "D306_Clock",
            "D306_Context",
            "EDA_Raw_Value",
            "Stress_Index",
            "IMU_Batch_Clock",
            "IMU_Batch_Context",
            "IMU_Sample_Count",
            "IMU_Samples_XYZ",
            "IMU_X0",
            "IMU_Y0",
            "IMU_Z0",
            "State_Code",
            "payload_hex",
            "full_packet_hex",
            "decoded_fields",
            "marker_label",
            "marker_source",
        ]
        computed_header = [
            "timestamp",
            "elapsed_ms",
            "device_mac",
            "connection_state",
            "data_type",
            "Source_D306_Clock",
            "Source_D306_Context",
            "Source_IMU_Batch_Clock",
            "Source_IMU_Batch_Context",
            "Skin_Resistance_kOhm",
            "Skin_Conductance_uS",
            "MM_Filtered_uS",
            "SCR_Frequency_Per_Min",
            "SCR_Amplitude",
            "MM_Arousal_Score",
            "MM_Calibrated",
            "IMU_Motion_Intensity",
            "D306_Observed_Hz",
            "IMU_Observed_Hz",
            "Rate_Target_Hz",
            "Rate_Control_Status",
            "Equalize_Mode",
            "Equalize_WouldDrop",
            "marker_label",
            "marker_source",
        ]

        try:
            if not state.stream_log_file:
                state.stream_log_file = self.log_dir / self._log_filename(
                    state, "streamed"
                )
                with open(
                    state.stream_log_file,
                    "w",
                    newline="",
                    encoding="utf-8",
                ) as file:
                    csv.writer(file).writerow(stream_header)
                if state.stream_log_queue:
                    state.stream_writer_task = asyncio.create_task(
                        self._csv_writer_loop(
                            state,
                            state.stream_log_queue,
                            state.stream_log_file,
                        )
                    )

            if not state.computed_log_file:
                state.computed_log_file = self.log_dir / self._log_filename(
                    state, "computed"
                )
                with open(
                    state.computed_log_file,
                    "w",
                    newline="",
                    encoding="utf-8",
                ) as file:
                    csv.writer(file).writerow(computed_header)
                if state.computed_log_queue:
                    state.computed_writer_task = asyncio.create_task(
                        self._csv_writer_loop(
                            state,
                            state.computed_log_queue,
                            state.computed_log_file,
                        )
                    )

            print(
                f"[LOG] Started split logs for {state.mac}: "
                f"{state.stream_log_file.name}, {state.computed_log_file.name}"
            )
        except Exception as e:
            print(f"[LOG] Error initializing split logs for {state.mac}: {e}")
            state.stream_log_file = None
            state.computed_log_file = None

    async def _csv_writer_loop(
        self,
        state: RingDeviceState,
        queue: asyncio.Queue[List[Any]],
        log_file: Path,
    ) -> None:
        if not log_file or not queue:
            return

        batch: List[List[Any]] = []
        while self.running or not queue.empty():
            try:
                row = await asyncio.wait_for(
                    queue.get(),
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
                log_file,
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

        if not state.log_file:
            self._initialize_log_file(state)

        try:
            state.log_queue.put_nowait(row)
        except asyncio.QueueFull:
            state.dropped_rows += 1

    def _enqueue_stream_log(self, state: RingDeviceState, row: List[Any]) -> None:
        if not self.enable_logging or not state.stream_log_queue:
            return

        if not state.stream_log_file or not state.computed_log_file:
            self._initialize_split_log_files(state)

        try:
            state.stream_log_queue.put_nowait(row)
        except asyncio.QueueFull:
            state.dropped_rows += 1

    def _enqueue_computed_log(self, state: RingDeviceState, row: List[Any]) -> None:
        if not self.enable_logging or not state.computed_log_queue:
            return

        if not state.stream_log_file or not state.computed_log_file:
            self._initialize_split_log_files(state)

        try:
            state.computed_log_queue.put_nowait(row)
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
                    mean_dt = sum(state.d306_intervals) / len(state.d306_intervals)
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
        last_ts = (
            state.last_accepted_d306_ts
            if stream_name == "d306"
            else state.last_accepted_imu_ts
        )
        if last_ts is None:
            return False

        current_dt = (datetime.now() - last_ts).total_seconds()
        should_drop = current_dt < target_dt

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
            (f"{state.d306_observed_hz:.3f}" if state.d306_observed_hz > 0 else ""),
            (f"{state.imu_observed_hz:.3f}" if state.imu_observed_hz > 0 else ""),
            f"{self.target_hz:.2f}" if self.target_hz else "",
            state.rate_control_status,
            self.equalize_mode,
            "1" if would_drop else "0",
        ]

    def add_marker(self, label: str, source: str = "manual") -> int:
        """Append a marker row to each active device log for later event alignment.

        Returns the number of device logs that received the marker.
        """
        clean_label = (label or "").strip() or "marker"
        marker_payload = json.dumps(
            {
                "label": clean_label,
                "source": source,
            },
            ensure_ascii=True,
        )

        marker_fields = [
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
            "",
            "",
            "",
            marker_payload,
        ]

        inserted = 0
        for state in self.device_states.values():
            row = (
                self._base_row(state, "MARKER")
                + marker_fields
                + self._row_rate_tail(state, would_drop=False)
            )
            self._enqueue_log(state, row)

            stream_row = self._base_row(state, "MARKER") + [
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
                clean_label,
                source,
            ]
            computed_row = self._base_row(state, "MARKER") + [
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
                "",
                "",
                "",
                clean_label,
                source,
            ]
            self._enqueue_stream_log(state, stream_row)
            self._enqueue_computed_log(state, computed_row)
            state.marker_count += 1
            inserted += 1

        return inserted

    def _make_stress_callback(self, mac: str):
        def _cb(_sender: Any, data: bytes) -> None:
            try:
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

                if would_drop and self.equalize_mode == "enforce":
                    return

                state.last_accepted_d306_ts = now
                state.d306_count += 1

                clock = parsed["clock"]
                context = parsed["context"]
                eda_value = parsed["eda_value"]
                dne_stress_index = parsed["dne_stress_index"]

                resistance_kohm, conductance_us = convert_eda(eda_value)
                filtered_us = state.signal_conditioner.process(conductance_us)
                freq, amp = state.scorer.update_scr_features(tonic_value=filtered_us)
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

                row = (
                    self._base_row(state, "D306_EDA")
                    + [
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
                    ]
                    + self._row_rate_tail(state, would_drop)
                )
                self._enqueue_log(state, row)

                stream_row = self._base_row(state, "D306_EDA") + [
                    clock,
                    context,
                    eda_value,
                    dne_stress_index,
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
                    "",
                    "",
                    "",
                ]
                computed_row = (
                    self._base_row(state, "D306_EDA_COMPUTED")
                    + [
                        clock,
                        context,
                        "",
                        "",
                        f"{resistance_kohm:.4f}",
                        f"{conductance_us:.4f}",
                        f"{filtered_us:.4f}",
                        f"{freq:.4f}",
                        f"{amp:.4f}",
                        f"{state.arousal_score:.2f}",
                        "1" if score_state["calibrated"] else "0",
                        "",
                    ]
                    + self._row_rate_tail(state, would_drop)
                    + [
                        "",
                        "",
                    ]
                )
                self._enqueue_stream_log(state, stream_row)
                self._enqueue_computed_log(state, computed_row)

                # Toggle the heartbeat for visual feedback
                state.heartbeat_tick = not state.heartbeat_tick
            except Exception:
                pass

        return _cb

    def _make_imu_callback(self, mac: str):
        def _cb(_sender: Any, data: bytes) -> None:
            try:
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

                if would_drop and self.equalize_mode == "enforce":
                    return

                state.last_accepted_imu_ts = now
                state.imu_batch_count += 1
                state.imu_xyz = (
                    parsed_batch["first_x"],
                    parsed_batch["first_y"],
                    parsed_batch["first_z"],
                )
                state.imu_batch_buffer.append(parsed_batch)

                row = (
                    self._base_row(state, "IMU_BATCH_468F")
                    + [
                        "",  # 5: EDA_Raw_Value
                        "",  # 6: Stress_Index
                        "",  # 7: MM_Filtered_uS
                        "",  # 8: MM_Arousal_Score
                        "",  # 9: MM_Calibrated
                        "",  # 10: Skin_Resistance_kOhm
                        "",  # 11: Skin_Conductance_uS
                        "",  # 12: D306_Clock
                        "",  # 13: D306_Context
                        parsed_batch["clock"],  # 14: IMU_Batch_Clock
                        parsed_batch["context"],  # 15: IMU_Batch_Context
                        parsed_batch["first_x"],  # 16: IMU_X0
                        parsed_batch["first_y"],  # 17: IMU_Y0
                        parsed_batch["first_z"],  # 18: IMU_Z0
                        f"{parsed_batch['motion_intensity']:.2f}",  # 19: IMU_Motion_Intensity
                        "",  # 20: State_Code
                        data[8:].hex(),  # 21: payload_hex
                        data.hex(),  # 22: full_packet_hex
                        "",  # 23: decoded_fields
                    ]
                    + self._row_rate_tail(state, would_drop)
                )
                self._enqueue_log(state, row)

                imu_samples_json = json.dumps(parsed_batch["samples"])
                stream_row = self._base_row(state, "IMU_BATCH_468F") + [
                    "",
                    "",
                    "",
                    "",
                    parsed_batch["clock"],
                    parsed_batch["context"],
                    len(parsed_batch["samples"]),
                    imu_samples_json,
                    parsed_batch["first_x"],
                    parsed_batch["first_y"],
                    parsed_batch["first_z"],
                    "",
                    data[8:].hex(),
                    data.hex(),
                    "",
                    "",
                    "",
                ]
                computed_row = (
                    self._base_row(state, "IMU_BATCH_468F_COMPUTED")
                    + [
                        "",
                        "",
                        parsed_batch["clock"],
                        parsed_batch["context"],
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        f"{parsed_batch['motion_intensity']:.2f}",
                    ]
                    + self._row_rate_tail(state, would_drop)
                    + [
                        "",
                        "",
                    ]
                )
                self._enqueue_stream_log(state, stream_row)
                self._enqueue_computed_log(state, computed_row)
            except Exception:
                pass

        return _cb

    def _make_raw_eda_callback(self, mac: str):
        def _cb(_sender: Any, data: bytes) -> None:
            try:
                if not self.capture_armed:
                    return

                state = self._ensure_device_state(mac)
                state.last_seen = datetime.now()
                state.state_count += 1
                state_code = data[0] if len(data) >= 1 else None
                would_drop = False

                row = (
                    self._base_row(state, "STATE_3C18")
                    + [
                        "",  # 5: EDA_Raw_Value
                        "",  # 6: Stress_Index
                        "",  # 7: MM_Filtered_uS
                        "",  # 8: MM_Arousal_Score
                        "",  # 9: MM_Calibrated
                        "",  # 10: Skin_Resistance_kOhm
                        "",  # 11: Skin_Conductance_uS
                        "",  # 12: D306_Clock
                        "",  # 13: D306_Context
                        "",  # 14: IMU_Batch_Clock
                        "",  # 15: IMU_Batch_Context
                        "",  # 16: IMU_X0
                        "",  # 17: IMU_Y0
                        "",  # 18: IMU_Z0
                        "",  # 19: IMU_Motion_Intensity
                        state_code if state_code is not None else "",  # 20: State_Code
                        data.hex(),  # 21: payload_hex
                        data.hex(),  # 22: full_packet_hex
                        "",  # 23: decoded_fields
                    ]
                    + self._row_rate_tail(state, would_drop)
                )
                self._enqueue_log(state, row)

                stream_row = self._base_row(state, "STATE_3C18") + [
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
                    "",
                    "",
                ]
                self._enqueue_stream_log(state, stream_row)
            except Exception:
                pass

        return _cb

    def _make_live_eda_callback(self, mac: str):
        def _cb(_sender: Any, data: bytes) -> None:
            try:
                if not self.capture_armed:
                    return

                state = self._ensure_device_state(mac)
                state.last_seen = datetime.now()
                state.live_eda_count += 1
                would_drop = False

                row = (
                    self._base_row(state, "LIVE_EDA_42DC")
                    + [
                        "",  # 5: EDA_Raw_Value
                        "",  # 6: Stress_Index
                        "",  # 7: MM_Filtered_uS
                        "",  # 8: MM_Arousal_Score
                        "",  # 9: MM_Calibrated
                        "",  # 10: Skin_Resistance_kOhm
                        "",  # 11: Skin_Conductance_uS
                        "",  # 12: D306_Clock
                        "",  # 13: D306_Context
                        "",  # 14: IMU_Batch_Clock
                        "",  # 15: IMU_Batch_Context
                        "",  # 16: IMU_X0
                        "",  # 17: IMU_Y0
                        "",  # 18: IMU_Z0
                        "",  # 19: IMU_Motion_Intensity
                        "",  # 20: State_Code
                        data.hex(),  # 21: payload_hex
                        data.hex(),  # 22: full_packet_hex
                        json.dumps({"len": len(data)}),  # 23: decoded_fields
                    ]
                    + self._row_rate_tail(state, would_drop)
                )
                self._enqueue_log(state, row)

                stream_row = self._base_row(state, "LIVE_EDA_42DC") + [
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
                    "",
                    "",
                ]
                self._enqueue_stream_log(state, stream_row)
            except Exception:
                pass

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

        # Optional Theory of Two: Firmware Warmup Sequence
        had_warmup = False
        if self.target_hz and self.attempt_ring_rate_control and self.use_warmup:
            print(
                f"[WARMUP] Priming firmware for Rate Control ({self.target_hz}Hz) on {mac}..."
            )
            warm_ok = await self.connector.connect_device(address=mac, device=device)
            if warm_ok:
                # Set the rate to kick the ring into gear, then disconnect
                await self.connector.attempt_set_sample_rate(
                    target_hz=int(self.target_hz),
                    address=mac,
                )
                print(
                    f"[WARMUP] Releasing {mac} to complete prime sequence... (delay: {self.warmup_delay}s)"
                )
                await self.connector.disconnect(address=mac)
                await asyncio.sleep(self.warmup_delay)
                had_warmup = True
            else:
                print(
                    f"[WARMUP] Failed initial prime connect for {mac}. Trying normal path."
                )

        ok = await self.connector.connect_device(address=mac, device=device)

        # Aggressive connection fallback if the OS link state is stuck
        if not ok and self.allow_reset_bt:
            print(
                f"[RECOVERY] Connection failed for {mac}. Trying aggressive BT radio reset..."
            )
            await self.connector._reset_bluetooth_radio()
            await asyncio.sleep(1.0)
            ok = await self.connector.connect_device(address=mac, device=device)
        elif not ok:
            print(
                f"[RECOVERY] Connection failed for {mac}. (Aggressive reset disabled)"
            )

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
            print(
                f"[INFO] {mac} Rate: {result.get('target_hz')}Hz | "
                f"Stat: {result.get('status')} | "
                f"P: {result.get('payload_hex')} | "
                f"E: {result.get('echo_hex')}"
            )
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
        scan_timeout: Optional[float] = None,
        scan_attempts: Optional[int] = None,
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

        # Hardware safety cap for multi-device sessions
        targets_count = len(ring_addresses or [])
        if monitor_all or targets_count > 1:
            if self.target_hz and self.target_hz > 16:
                if self.force_hz:
                    print(
                        f"[DANGER] Multi-ring Hz safety cap bypassed "
                        f"via force: {self.target_hz} Hz"
                    )
                else:
                    print(
                        f"[WARN] Multi-ring sessions are unstable above ~16 Hz due to hardware limitations. "
                        f"Capping {self.target_hz} Hz -> 16.0 Hz."
                    )
                    self.target_hz = 16.0

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

            self._health_task = asyncio.create_task(self._connection_health_loop())
            return True

        # Multi-device path.
        targets = [a.upper() for a in (ring_addresses or [])]
        discovered_by_mac = {}

        if monitor_all or not targets:
            # Use SDK defaults if not specified
            s_timeout = scan_timeout if scan_timeout is not None else 6.0
            s_attempts = scan_attempts if scan_attempts is not None else 3

            discovered: List[Dict[str, Any]] = (
                await self.connector.discover_all_matching_rings(
                    include_device=True,
                    scan_timeout=s_timeout,
                    attempts=s_attempts,
                    retry_delay=0.5,
                    stop_if_found=True,
                )
            )

            if (
                not discovered
                and platform.system() == "Windows"
                and self.allow_reset_bt
            ):
                print(
                    "[BT-RESET] No rings discovered and allow_reset_bt is enabled. "
                    "Resetting Bluetooth adapter to clear stale connections..."
                )
                reset_ok = await self.connector._reset_bluetooth_radio()
                if reset_ok:
                    print("[BT-RESET] Rescanning after adapter reset...")
                    discovered = await self.connector.discover_all_matching_rings(
                        include_device=True,
                        scan_timeout=s_timeout,
                        attempts=s_attempts,
                        retry_delay=0.5,
                        stop_if_found=True,
                    )

            discovered_by_mac = {d["address"].upper(): d for d in discovered}
            if not targets:
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
            try:
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
            except Exception:
                pass

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

        # Surface dropped-row warnings at session end
        for state in self.device_states.values():
            for task in (
                state.writer_task,
                state.stream_writer_task,
                state.computed_writer_task,
            ):
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            if state.dropped_rows > 0:
                print(
                    f"[WARN] {state.mac}: {state.dropped_rows} log rows "
                    f"were dropped (queue full). Consider reducing target_hz "
                    f"or increasing queue size."
                )

    def dashboard_rows(self) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        for mac, state in self.device_states.items():
            bat_str = f"{state.battery}%" if state.battery else "-"
            eda_str = str(state.raw_eda) if state.raw_eda else "N/A"
            filt_str = f"{state.filtered_us:.3f}" if state.filtered_us else "N/A"
            ar_str = f"{state.arousal_score:.1f}"
            dne_str = (
                str(state.dne_stress_index)
                if state.dne_stress_index is not None
                else "N/A"
            )
            rate_hz = f"{state.d306_observed_hz:.1f}/{state.imu_observed_hz:.1f}"
            hb_mark = "*" if state.heartbeat_tick else " "
            rate_hz = f"{hb_mark} {rate_hz}"
            drop_info = f" DROP:{state.dropped_rows}" if state.dropped_rows > 0 else ""

            imu_x, imu_y, imu_z = state.imu_xyz
            rows.append(
                {
                    "device_mac": mac,
                    "connection_status": state.status + drop_info,
                    "battery": bat_str,
                    "raw_eda": eda_str,
                    "filtered_us": filt_str,
                    "arousal_score": ar_str,
                    "dne_score": dne_str,
                    "observed_hz": rate_hz,
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

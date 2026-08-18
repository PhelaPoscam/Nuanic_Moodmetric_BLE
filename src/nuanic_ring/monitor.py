"""Real-time multi-ring monitor and coordinator facade for Nuanic ring streams."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from nuanic_ring.core.connector import NuanicConnector
from nuanic_ring.dsp.signal_processing import SignalConditioner
from nuanic_ring.io.manifest import generate_session_manifest
from nuanic_ring.io.schemas import (
    CombinedLogRow,
    ComputedLogRow,
    D306Packet,
    FingerStatePacket,
    ImuBatchPacket,
    ImuLogRow,
    LiveEdaPacket,
    NuanicExportLogRow,
    StreamLogRow,
)
from nuanic_ring.io.writers import (
    build_log_filename,
    csv_writer_loop,
    drain_writer_tasks,
    open_csv_log_file,
)
from nuanic_ring.telemetry.callbacks import (
    parse_468f_imu_batch,
    parse_d306_packet,
    parse_finger_state_packet,
    parse_live_eda_packet,
)
from nuanic_ring.telemetry.device_state import RingDeviceState
from nuanic_ring.telemetry.rate_control import (
    build_row_rate_tail,
    equalize_decision,
    update_observed_hz,
)
from nuanic_ring.telemetry.time_sync import get_smoothed_time, nuanic_ts_fields

_log = logging.getLogger(__name__)


class NuanicMonitor:
    """Multi-device monitor coordinator with isolated per-device state and logging."""

    def __init__(
        self,
        log_dir: str = "data/ring_logs",
        imu_refresh_packets: int = 5,
        clear_console: bool = True,
        enable_logging: bool = True,
        csv_layout: str = "combined",
        target_hz: Optional[float] = None,
        equalize_mode: str = "off",
        attempt_ring_rate_control: bool = False,
        allow_reset_bt: bool = False,
        participant_id: Optional[str] = None,
        apply_filter: bool = False,
        initial_mode: Optional[int] = None,
        use_warmup: bool = False,
        warmup_delay: float = 1.0,
        force_hz: bool = False,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.imu_refresh_packets = imu_refresh_packets
        self.clear_console = clear_console
        self.enable_logging = enable_logging
        self.csv_layout = csv_layout
        self.target_hz = target_hz
        self.equalize_mode = equalize_mode
        self.attempt_ring_rate_control = attempt_ring_rate_control
        self.allow_reset_bt = allow_reset_bt
        self.participant_id = participant_id
        self.use_warmup = use_warmup
        self.warmup_delay = warmup_delay
        self.force_hz = force_hz

        self.connector = NuanicConnector()
        self.apply_filter = apply_filter
        self.initial_mode = initial_mode

        self.session_timestamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
        self.start_time: Optional[datetime] = None
        self.running = False
        self.capture_armed = False
        self.device_states: Dict[str, RingDeviceState] = {}

        self._health_task: Optional[asyncio.Task[None]] = None
        self._auto_reconnect = True
        self._reconnect_backoff_seconds = 2.0

    def _ensure_device_state(self, mac: str) -> RingDeviceState:
        """Fetch or initialize a per-device runtime state container."""
        mac = mac.upper()
        if mac not in self.device_states:
            state = RingDeviceState(mac=mac)
            if self.enable_logging:
                state.log_queue = asyncio.Queue(maxsize=4000)
                state.stream_log_queue = asyncio.Queue(maxsize=4000)
                state.computed_log_queue = asyncio.Queue(maxsize=4000)
                state.imu_log_queue = asyncio.Queue(maxsize=4000)
            self.device_states[mac] = state
            if self.running:
                self._initialize_log_file(state)
                self._initialize_split_log_files(state)
                self._initialize_imu_log_file(state)
        return self.device_states[mac]

    def _get_signal_conditioner(self, state: RingDeviceState) -> SignalConditioner:
        """Return the per-device signal conditioner, lazy-initialized with observed Hz."""
        if state.signal_conditioner is None:
            observed = max(1.0, state.d306_observed_hz or 8.0)
            state.signal_conditioner = SignalConditioner(sample_rate=observed)
        return state.signal_conditioner

    def _elapsed_seconds(self) -> float:
        if not self.start_time:
            return 0.0
        return max(0.001, (datetime.now() - self.start_time).total_seconds())

    def _get_smoothed_time(
        self,
        state: RingDeviceState,
        stream: str,
        clock: int,
    ) -> Tuple[datetime, int]:
        return get_smoothed_time(state, stream, clock, start_time=self.start_time)

    @staticmethod
    def _nuanic_ts_fields(ts: datetime) -> Tuple[str, str]:
        return nuanic_ts_fields(ts)

    def _parse_d306_packet(self, data: bytes) -> Optional[Dict[str, Any]]:
        return parse_d306_packet(data)

    def _parse_468f_imu_batch(self, data: bytes) -> Optional[Dict[str, Any]]:
        return parse_468f_imu_batch(data)

    def _log_filename(self, state: RingDeviceState, suffix: str = "") -> str:
        return build_log_filename(state.mac, suffix, self.participant_id)

    def _open_log_file(
        self, state: RingDeviceState, suffix: str, header: List[str]
    ) -> Path | None:
        return open_csv_log_file(
            log_dir=self.log_dir,
            session_timestamp=self.session_timestamp,
            mac=state.mac,
            suffix=suffix,
            header=header,
            participant_id=self.participant_id,
            enabled=self.enable_logging,
        )

    def _start_writer(
        self, state: RingDeviceState, queue: asyncio.Queue[List[Any]], file_path: Path
    ) -> Optional[asyncio.Task[None]]:
        writer = self._csv_writer_loop(state, queue, file_path)
        try:
            return asyncio.create_task(writer)
        except RuntimeError:
            writer.close()
            return None

    def _initialize_log_file(self, state: RingDeviceState) -> None:
        """Initialize unified combined CSV log file."""
        if not state.log_queue or (state.log_file and state.writer_task):
            return
        header = (
            NuanicExportLogRow.header()
            if self.csv_layout == "nuanic"
            else CombinedLogRow.header()
        )
        if not state.log_file:
            state.log_file = self._open_log_file(state, "", header)
        if state.log_file and state.log_queue:
            state.writer_task = self._start_writer(
                state, state.log_queue, state.log_file
            )

    def _initialize_split_log_files(self, state: RingDeviceState) -> None:
        """Initialize split raw-stream and computed CSV log files."""
        if not state.stream_log_queue or not state.computed_log_queue:
            return
        if (
            state.stream_log_file
            and state.computed_log_file
            and (state.stream_writer_task and state.computed_writer_task)
        ):
            return
        self._initialize_split_log_files_helper(state)

    def _initialize_split_log_files_helper(self, state: RingDeviceState) -> None:
        stream_header = StreamLogRow.header()
        computed_header = ComputedLogRow.header()
        if not state.stream_log_file:
            state.stream_log_file = self._open_log_file(
                state, "streamed", stream_header
            )
        if state.stream_log_file and state.stream_log_queue:
            state.stream_writer_task = self._start_writer(
                state, state.stream_log_queue, state.stream_log_file
            )
        if not state.computed_log_file:
            state.computed_log_file = self._open_log_file(
                state, "computed", computed_header
            )
        if state.computed_log_file and state.computed_log_queue:
            state.computed_writer_task = self._start_writer(
                state, state.computed_log_queue, state.computed_log_file
            )

    def _initialize_imu_log_file(self, state: RingDeviceState) -> None:
        """Initialize dedicated IMU CSV log file."""
        if not state.imu_log_queue or (state.imu_log_file and state.imu_writer_task):
            return
        header = ImuLogRow.header()
        if not state.imu_log_file:
            state.imu_log_file = self._open_log_file(state, "imu", header)
        if state.imu_log_file and state.imu_log_queue:
            state.imu_writer_task = self._start_writer(
                state, state.imu_log_queue, state.imu_log_file
            )

    async def _csv_writer_loop(
        self,
        state: RingDeviceState,
        queue: asyncio.Queue[List[Any]],
        log_file: Path,
    ) -> None:
        await csv_writer_loop(
            is_running=lambda: self.running,
            queue=queue,
            log_file=log_file,
        )

    def _enqueue_to(
        self,
        state: RingDeviceState,
        queue: Optional[asyncio.Queue[List[Any]]],
        row: List[Any],
        file_ready: bool,
        initializer: Callable[[RingDeviceState], None],
    ) -> None:
        """Guard, lazy-init, and enqueue a CSV row; record drops if full."""
        if not self.enable_logging or not queue:
            return
        if not file_ready:
            initializer(state)
        try:
            queue.put_nowait(row)
        except asyncio.QueueFull:
            state.dropped_rows += 1

    def _enqueue_log(self, state: RingDeviceState, row: List[Any]) -> None:
        self._enqueue_to(
            state,
            state.log_queue,
            row,
            file_ready=bool(state.log_file),
            initializer=self._initialize_log_file,
        )

    def _enqueue_stream_log(self, state: RingDeviceState, row: List[Any]) -> None:
        self._enqueue_to(
            state,
            state.stream_log_queue,
            row,
            file_ready=bool(state.stream_log_file and state.computed_log_file),
            initializer=self._initialize_split_log_files,
        )

    def _enqueue_computed_log(self, state: RingDeviceState, row: List[Any]) -> None:
        self._enqueue_to(
            state,
            state.computed_log_queue,
            row,
            file_ready=bool(state.stream_log_file and state.computed_log_file),
            initializer=self._initialize_split_log_files,
        )

    def _enqueue_imu_log(self, state: RingDeviceState, row: List[Any]) -> None:
        self._enqueue_to(
            state,
            state.imu_log_queue,
            row,
            file_ready=bool(state.imu_log_file),
            initializer=self._initialize_imu_log_file,
        )

    def _base_row(
        self,
        state: RingDeviceState,
        data_type: str,
        custom_ts: Optional[datetime] = None,
        custom_elapsed: Optional[int] = None,
    ) -> List[Any]:
        if custom_ts is not None:
            timestamp = custom_ts.isoformat(timespec="milliseconds")
        else:
            timestamp = datetime.now().isoformat(timespec="milliseconds")

        if custom_elapsed is not None:
            elapsed_ms = custom_elapsed
        else:
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
        update_observed_hz(state, stream_name, now)

    def _equalize_decision(
        self,
        state: RingDeviceState,
        stream_name: str,
    ) -> bool:
        return equalize_decision(
            state,
            stream_name,
            target_hz=self.target_hz,
            equalize_mode=self.equalize_mode,
        )

    def _row_rate_tail(
        self,
        state: RingDeviceState,
        would_drop: bool,
    ) -> List[Any]:
        return build_row_rate_tail(
            state,
            target_hz=self.target_hz,
            equalize_mode=self.equalize_mode,
            would_drop=would_drop,
        )

    def add_marker(self, label: str, source: str = "manual") -> int:
        """Append an event marker row to each active device log for later synchronization."""
        clean_label = (label or "").strip() or "marker"
        marker_payload = json.dumps(
            {
                "label": clean_label,
                "source": source,
            },
            ensure_ascii=True,
        )

        inserted = 0
        for state in self.device_states.values():
            ts_iso = datetime.now().isoformat(timespec="milliseconds")
            elapsed_ms = int(self._elapsed_seconds() * 1000)
            rate_tail = self._row_rate_tail(state, would_drop=False)

            if self.csv_layout != "nuanic":
                row = CombinedLogRow(
                    timestamp=ts_iso,
                    elapsed_ms=elapsed_ms,
                    device_mac=state.mac,
                    connection_state=state.status,
                    data_type="MARKER",
                    decoded_fields=marker_payload,
                    D306_Observed_Hz=rate_tail[0],
                    IMU_Observed_Hz=rate_tail[1],
                    Rate_Target_Hz=rate_tail[2],
                    Rate_Control_Status=rate_tail[3],
                    Equalize_Mode=rate_tail[4],
                    Equalize_WouldDrop=rate_tail[5],
                ).to_csv_row()
                self._enqueue_log(state, row)

            stream_row = StreamLogRow(
                timestamp=ts_iso,
                elapsed_ms=elapsed_ms,
                device_mac=state.mac,
                connection_state=state.status,
                data_type="MARKER",
                marker_label=clean_label,
                marker_source=source,
            ).to_csv_row()

            computed_row = ComputedLogRow(
                timestamp=ts_iso,
                elapsed_ms=elapsed_ms,
                device_mac=state.mac,
                connection_state=state.status,
                data_type="MARKER",
                D306_Observed_Hz=rate_tail[0],
                IMU_Observed_Hz=rate_tail[1],
                Rate_Target_Hz=rate_tail[2],
                Rate_Control_Status=rate_tail[3],
                Equalize_Mode=rate_tail[4],
                Equalize_WouldDrop=rate_tail[5],
                marker_label=clean_label,
                marker_source=source,
            ).to_csv_row()

            self._enqueue_stream_log(state, stream_row)
            self._enqueue_computed_log(state, computed_row)

            imu_marker = ImuLogRow(
                timestamp=ts_iso,
                elapsed_ms=elapsed_ms,
                clock=0,
                context=0,
                motion_intensity="",
                x=0,
                y=0,
                z=0,
                marker=marker_payload,
            ).to_csv_row()
            # Replace placeholder zeros with empty string for marker row
            imu_marker[2] = ""
            imu_marker[3] = ""
            imu_marker[5] = ""
            imu_marker[6] = ""
            imu_marker[7] = ""
            self._enqueue_imu_log(state, imu_marker)

            state.marker_count += 1
            inserted += 1

        return inserted

    def _make_stress_callback(self, mac: str):
        """Dispatcher for LIVE_DNE stream (d306262b...)."""

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

                resistance_kohm = eda_value / 1000.0
                conductance_us = (
                    (1000.0 / resistance_kohm) if resistance_kohm > 0 else 0.0
                )
                filtered_us = (
                    self._get_signal_conditioner(state).process(conductance_us)
                    if self.apply_filter
                    else conductance_us
                )
                freq, amp = 0.0, 0.0
                state.raw_eda = eda_value
                state.filtered_us = filtered_us
                state.dne_stress_index = dne_stress_index
                state.arousal_score = (
                    float(dne_stress_index) if dne_stress_index is not None else 0.0
                )
                state.d306_buffer.append(
                    {
                        "clock": clock,
                        "context": context,
                        "eda_value": eda_value,
                        "dne_stress_index": dne_stress_index,
                    }
                )
                state.mm_filtered_us_wave.append(filtered_us)
                state.mm_arousal_wave.append(state.arousal_score)
                state.live_dna_index.append(state.d306_count)
                state.live_dna_word2.append(eda_value)
                state.dne_stress_index_wave.append(dne_stress_index)
                smoothed_ts, elapsed_ms = self._get_smoothed_time(state, "d306", clock)
                ts_iso = smoothed_ts.isoformat(timespec="milliseconds")
                rate_tail = self._row_rate_tail(state, would_drop)

                if self.csv_layout == "nuanic":
                    ts_unix, ts_str = self._nuanic_ts_fields(smoothed_ts)
                    srl_ohms = int(1_000_000 / filtered_us) if filtered_us > 0 else 0
                    srrn = f"{freq:.1f}"
                    row = NuanicExportLogRow(
                        address=state.mac,
                        time_unix=ts_unix,
                        time=ts_str,
                        dne=dne_stress_index,
                        srl=srl_ohms,
                        srrn=srrn,
                        eda=eda_value,
                    ).to_csv_row()
                    self._enqueue_log(state, row)
                else:
                    row = CombinedLogRow(
                        timestamp=ts_iso,
                        elapsed_ms=elapsed_ms,
                        device_mac=state.mac,
                        connection_state=state.status,
                        data_type="D306_EDA",
                        EDA_Raw_Value=eda_value,
                        Stress_Index=dne_stress_index,
                        D306_Clock=clock,
                        D306_Context=context,
                        State_Code="",
                        payload_hex=data.hex(),
                        full_packet_hex=data.hex(),
                        decoded_fields="",
                        D306_Observed_Hz=rate_tail[0],
                        IMU_Observed_Hz=rate_tail[1],
                        Rate_Target_Hz=rate_tail[2],
                        Rate_Control_Status=rate_tail[3],
                        Equalize_Mode=rate_tail[4],
                        Equalize_WouldDrop=rate_tail[5],
                    ).to_csv_row()
                    self._enqueue_log(state, row)

                stream_row = StreamLogRow(
                    timestamp=ts_iso,
                    elapsed_ms=elapsed_ms,
                    device_mac=state.mac,
                    connection_state=state.status,
                    data_type="D306_EDA",
                    D306_Clock=clock,
                    D306_Context=context,
                    EDA_Raw_Value=eda_value,
                    Stress_Index=dne_stress_index,
                    State_Code="",
                    payload_hex=data.hex(),
                    full_packet_hex=data.hex(),
                    decoded_fields="",
                    marker_label="",
                    marker_source="",
                ).to_csv_row()

                computed_row = ComputedLogRow(
                    timestamp=ts_iso,
                    elapsed_ms=elapsed_ms,
                    device_mac=state.mac,
                    connection_state=state.status,
                    data_type="D306_EDA_COMPUTED",
                    Source_D306_Clock=clock,
                    Source_D306_Context=context,
                    Skin_Resistance_kOhm=f"{resistance_kohm:.4f}",
                    Skin_Conductance_uS=f"{conductance_us:.4f}",
                    MM_Filtered_uS=f"{filtered_us:.4f}",
                    SCR_Frequency_Per_Min=f"{freq:.4f}",
                    SCR_Amplitude=f"{amp:.4f}",
                    MM_Arousal_Score=f"{state.arousal_score:.2f}",
                    MM_Calibrated="1",
                    D306_Observed_Hz=rate_tail[0],
                    IMU_Observed_Hz=rate_tail[1],
                    Rate_Target_Hz=rate_tail[2],
                    Rate_Control_Status=rate_tail[3],
                    Equalize_Mode=rate_tail[4],
                    Equalize_WouldDrop=rate_tail[5],
                    marker_label="",
                    marker_source="",
                ).to_csv_row()
                self._enqueue_stream_log(state, stream_row)
                self._enqueue_computed_log(state, computed_row)

                state.heartbeat_tick = not state.heartbeat_tick
            except Exception:
                _log.debug("Stress callback error for %s", mac, exc_info=True)

        return _cb

    def _make_imu_callback(self, mac: str):
        """Dispatcher for IMU batch stream (468f2717...)."""

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
                state.imu_index.append(state.imu_batch_count)
                state.imu_intensity.append(parsed_batch["motion_intensity"])

                smoothed_ts, elapsed_ms = self._get_smoothed_time(
                    state, "imu", parsed_batch["clock"]
                )

                timestamp_iso = smoothed_ts.isoformat(timespec="milliseconds")
                for x, y, z in parsed_batch["samples"]:
                    imu_row = ImuLogRow(
                        timestamp=timestamp_iso,
                        elapsed_ms=elapsed_ms,
                        clock=parsed_batch["clock"],
                        context=parsed_batch["context"],
                        motion_intensity=f"{parsed_batch['motion_intensity']:.4f}",
                        x=x,
                        y=y,
                        z=z,
                        marker="",
                    ).to_csv_row()
                    self._enqueue_imu_log(state, imu_row)

            except Exception:
                _log.debug("IMU callback error for %s", mac, exc_info=True)

        return _cb

    def _make_raw_eda_callback(self, mac: str):
        """Dispatcher for STATE on-finger contact stream (3c180fcc...)."""

        def _cb(_sender: Any, data: bytes) -> None:
            try:
                if not self.capture_armed:
                    return

                state = self._ensure_device_state(mac)
                state.last_seen = datetime.now()
                state.state_count += 1
                state_code = data[0] if len(data) >= 1 else None
                would_drop = False
                ts_iso = datetime.now().isoformat(timespec="milliseconds")
                elapsed_ms = int(self._elapsed_seconds() * 1000)
                rate_tail = self._row_rate_tail(state, would_drop)

                if self.csv_layout != "nuanic":
                    row = CombinedLogRow(
                        timestamp=ts_iso,
                        elapsed_ms=elapsed_ms,
                        device_mac=state.mac,
                        connection_state=state.status,
                        data_type="STATE_3C18",
                        EDA_Raw_Value="",
                        Stress_Index="",
                        D306_Clock="",
                        D306_Context="",
                        State_Code=state_code if state_code is not None else "",
                        payload_hex=data.hex(),
                        full_packet_hex=data.hex(),
                        decoded_fields="",
                        D306_Observed_Hz=rate_tail[0],
                        IMU_Observed_Hz=rate_tail[1],
                        Rate_Target_Hz=rate_tail[2],
                        Rate_Control_Status=rate_tail[3],
                        Equalize_Mode=rate_tail[4],
                        Equalize_WouldDrop=rate_tail[5],
                    ).to_csv_row()
                    self._enqueue_log(state, row)

                stream_row = StreamLogRow(
                    timestamp=ts_iso,
                    elapsed_ms=elapsed_ms,
                    device_mac=state.mac,
                    connection_state=state.status,
                    data_type="STATE_3C18",
                    D306_Clock="",
                    D306_Context="",
                    EDA_Raw_Value="",
                    Stress_Index="",
                    State_Code=state_code if state_code is not None else "",
                    payload_hex=data.hex(),
                    full_packet_hex=data.hex(),
                    decoded_fields="",
                    marker_label="",
                    marker_source="",
                ).to_csv_row()
                self._enqueue_stream_log(state, stream_row)
            except Exception:
                _log.debug("Raw EDA callback error for %s", mac, exc_info=True)

        return _cb

    def _make_live_eda_callback(self, mac: str):
        """Dispatcher for LIVE_EDA uncalibrated raw resistance stream (42dcb71b...)."""

        def _cb(_sender: Any, data: bytes) -> None:
            try:
                if not self.capture_armed:
                    return

                state = self._ensure_device_state(mac)
                now = datetime.now()
                state.last_seen = now
                self._update_observed_hz(state, "d306", now)
                would_drop = self._equalize_decision(state, "d306")

                if would_drop and self.equalize_mode == "enforce":
                    return

                state.last_accepted_d306_ts = now
                state.live_eda_count += 1
                state.heartbeat_tick = not state.heartbeat_tick

                decoded: Dict[str, Any] = {"len": len(data)}
                eda_str = ""
                freq, amp = 0.0, 0.0
                res_kohm, cond_us, filtered_us = 0.0, 0.0, 0.0
                timestamp_ms = ""

                if len(data) == 14:
                    parsed_eda = parse_live_eda_packet(data)
                    if parsed_eda:
                        decoded = parsed_eda
                        timestamp_ms = str(parsed_eda["timestamp_ms"])
                        eda_ohm = parsed_eda["eda_ohm"]
                        res_kohm = parsed_eda["resistance_kohm"]
                        cond_us = parsed_eda["conductance_us"]
                        eda_str = str(eda_ohm)

                        state.raw_eda = eda_ohm
                        filtered_us = (
                            self._get_signal_conditioner(state).process(cond_us)
                            if self.apply_filter
                            else cond_us
                        )
                        state.filtered_us = filtered_us
                        state.mm_filtered_us_wave.append(filtered_us)
                        state.live_dna_index.append(state.live_eda_count)
                        state.live_dna_word2.append(eda_ohm)
                        state.dne_stress_index = 0
                        state.dne_stress_index_wave.append(0.0)

                        freq, amp = 0.0, 0.0
                        state.arousal_score = 0.0
                        state.mm_arousal_wave.append(0.0)

                ts_iso = now.isoformat(timespec="milliseconds")
                elapsed_ms = int(self._elapsed_seconds() * 1000)
                rate_tail = self._row_rate_tail(state, would_drop)

                if self.csv_layout == "nuanic":
                    if timestamp_ms:
                        smoothed_ts, _ = self._get_smoothed_time(
                            state, "d306", int(timestamp_ms)
                        )
                    else:
                        smoothed_ts = now
                    ts_unix, ts_str = self._nuanic_ts_fields(smoothed_ts)
                    srl_ohms = int(eda_ohm) if len(data) == 14 else 0
                    srrn = f"{freq:.1f}" if len(data) == 14 else "0.0"
                    eda_val = int(eda_ohm) if len(data) == 14 else 0
                    row = NuanicExportLogRow(
                        address=state.mac,
                        time_unix=ts_unix,
                        time=ts_str,
                        dne=0,
                        srl=srl_ohms,
                        srrn=srrn,
                        eda=eda_val,
                    ).to_csv_row()
                    self._enqueue_log(state, row)
                else:
                    row = CombinedLogRow(
                        timestamp=ts_iso,
                        elapsed_ms=elapsed_ms,
                        device_mac=state.mac,
                        connection_state=state.status,
                        data_type="LIVE_EDA_42DC",
                        EDA_Raw_Value=eda_str,
                        Stress_Index="0",
                        D306_Clock=str(timestamp_ms),
                        D306_Context="",
                        State_Code="",
                        payload_hex=data.hex(),
                        full_packet_hex=data.hex(),
                        decoded_fields=json.dumps(decoded),
                        D306_Observed_Hz=rate_tail[0],
                        IMU_Observed_Hz=rate_tail[1],
                        Rate_Target_Hz=rate_tail[2],
                        Rate_Control_Status=rate_tail[3],
                        Equalize_Mode=rate_tail[4],
                        Equalize_WouldDrop=rate_tail[5],
                    ).to_csv_row()
                    self._enqueue_log(state, row)

                    stream_row = StreamLogRow(
                        timestamp=ts_iso,
                        elapsed_ms=elapsed_ms,
                        device_mac=state.mac,
                        connection_state=state.status,
                        data_type="LIVE_EDA_42DC",
                        D306_Clock=str(timestamp_ms),
                        D306_Context="",
                        EDA_Raw_Value=eda_str,
                        Stress_Index="0",
                        State_Code="",
                        payload_hex=data.hex(),
                        full_packet_hex=data.hex(),
                        decoded_fields=json.dumps(decoded),
                        marker_label="",
                        marker_source="",
                    ).to_csv_row()

                    computed_row = ComputedLogRow(
                        timestamp=ts_iso,
                        elapsed_ms=elapsed_ms,
                        device_mac=state.mac,
                        connection_state=state.status,
                        data_type="LIVE_EDA_COMPUTED",
                        Source_D306_Clock=str(timestamp_ms),
                        Source_D306_Context="",
                        Skin_Resistance_kOhm=(
                            f"{res_kohm:.4f}" if len(data) == 14 else ""
                        ),
                        Skin_Conductance_uS=f"{cond_us:.4f}" if len(data) == 14 else "",
                        MM_Filtered_uS=f"{filtered_us:.4f}" if len(data) == 14 else "",
                        SCR_Frequency_Per_Min=f"{freq:.4f}" if len(data) == 14 else "",
                        SCR_Amplitude=f"{amp:.4f}" if len(data) == 14 else "",
                        MM_Arousal_Score=(
                            f"{state.arousal_score:.2f}" if len(data) == 14 else ""
                        ),
                        MM_Calibrated="1",
                        D306_Observed_Hz=rate_tail[0],
                        IMU_Observed_Hz=rate_tail[1],
                        Rate_Target_Hz=rate_tail[2],
                        Rate_Control_Status=rate_tail[3],
                        Equalize_Mode=rate_tail[4],
                        Equalize_WouldDrop=rate_tail[5],
                        marker_label="",
                        marker_source="",
                    ).to_csv_row()
                    self._enqueue_stream_log(state, stream_row)
                    self._enqueue_computed_log(state, computed_row)
            except Exception:
                _log.debug("Live EDA callback error for %s", mac, exc_info=True)

        return _cb

    async def _subscribe_device_streams(self, mac: str) -> bool:
        sub_imu = getattr(
            self.connector, "subscribe_to_imu_motion", self.connector.subscribe_to_imu
        )
        sub_dne = getattr(
            self.connector, "subscribe_to_live_dne", self.connector.subscribe_to_stress
        )
        sub_finger = getattr(
            self.connector,
            "subscribe_to_finger_state",
            self.connector.subscribe_to_raw_eda,
        )
        sub_eda_ohms = getattr(
            self.connector,
            "subscribe_to_raw_eda_ohms",
            self.connector.subscribe_to_live_eda,
        )
        results = await asyncio.gather(
            sub_imu(self._make_imu_callback(mac), address=mac),
            sub_dne(self._make_stress_callback(mac), address=mac),
            sub_finger(self._make_raw_eda_callback(mac), address=mac),
            sub_eda_ohms(self._make_live_eda_callback(mac), address=mac),
        )
        return all(results)

    async def _unsubscribe_device_streams(self, mac: str) -> None:
        unsub_imu = getattr(
            self.connector,
            "unsubscribe_from_imu_motion",
            self.connector.unsubscribe_from_imu,
        )
        unsub_dne = getattr(
            self.connector,
            "unsubscribe_from_live_dne",
            self.connector.unsubscribe_from_stress,
        )
        unsub_finger = getattr(
            self.connector,
            "unsubscribe_from_finger_state",
            self.connector.unsubscribe_from_raw_eda,
        )
        unsub_eda_ohms = getattr(
            self.connector,
            "unsubscribe_from_raw_eda_ohms",
            self.connector.unsubscribe_from_live_eda,
        )
        await asyncio.gather(
            unsub_imu(address=mac),
            unsub_dne(address=mac),
            unsub_finger(address=mac),
            unsub_eda_ohms(address=mac),
        )

    async def _warmup_sequence(self, mac: str, device: Any = None) -> None:
        """Optional firmware warmup: prime the ring at target rate, then release."""
        if not (self.target_hz and self.attempt_ring_rate_control and self.use_warmup):
            return
        _log.info(
            f"[WARMUP] Priming firmware for Rate Control ({self.target_hz}Hz) on {mac}..."
        )
        warm_ok = await self.connector.connect_device(address=mac, device=device)
        if warm_ok:
            await self.connector.attempt_set_sample_rate(
                target_hz=int(self.target_hz),
                address=mac,
            )
            _log.info(
                f"[WARMUP] Releasing {mac} to complete prime sequence... (delay: {self.warmup_delay}s)"
            )
            await self.connector.disconnect(address=mac)
            await asyncio.sleep(self.warmup_delay)
        else:
            _log.info(
                f"[WARMUP] Failed initial prime connect for {mac}. Trying normal path."
            )

    async def _connect_and_subscribe(
        self,
        mac: str,
        device: Any = None,
    ) -> bool:
        state = self._ensure_device_state(mac)
        state.status = "connecting"

        await self._warmup_sequence(mac, device)

        ok = await self.connector.connect_device(address=mac, device=device)

        if not ok and self.allow_reset_bt:
            _log.info(
                f"[RECOVERY] Connection failed for {mac}. Trying aggressive BT radio reset..."
            )
            await self.connector._reset_bluetooth_radio()
            await asyncio.sleep(1.5)
            discovered = await self.connector.discover_all_matching_rings(
                include_device=True, stop_if_found=True
            )
            fresh_device = next(
                (
                    d.get("device")
                    for d in discovered
                    if d["address"].upper() == mac.upper()
                ),
                None,
            )
            ok = await self.connector.connect_device(
                address=mac, device=fresh_device or device
            )
        elif not ok:
            _log.info(
                f"[RECOVERY] Connection failed for {mac}. (Aggressive reset disabled)"
            )

        if not ok:
            state.status = "disconnected"
            return False

        state.status = "connected"
        state.reconnect_attempt = 0
        bat = await self.connector.read_battery(address=mac)
        state.battery = bat
        if bat is not None:
            if state.battery_start is None:
                state.battery_start = bat
            state.battery_end = bat

        await self._post_connect_setup(mac, state)

        streams_ok = await self._subscribe_device_streams(mac)
        if not streams_ok:
            state.status = "degraded"
            return False

        return True

    async def _post_connect_setup(self, mac: str, state: Any) -> None:
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
            _log.info(
                f"[INFO] {mac} Rate: {result.get('target_hz')}Hz | "
                f"Stat: {result.get('status')} | "
                f"P: {result.get('payload_hex')} | "
                f"E: {result.get('echo_hex')}"
            )
        elif self.target_hz:
            state.rate_control_status = "not-requested"
        else:
            state.rate_control_status = "not-configured"

        if self.initial_mode is not None:
            label = NuanicConnector.MODE_LABELS.get(self.initial_mode & 0xFF, "?")
            _log.info(
                "[MODE] Setting ring %s to 0x%02X (%s) — expect 60s calibration silence",
                mac,
                self.initial_mode & 0xFF,
                label,
            )
            await self.connector.set_mode(self.initial_mode, address=mac)

    def _apply_multi_ring_hz_cap(self, is_multi: bool) -> None:
        """Hardware safety cap: multi-ring sessions are unstable above ~16 Hz."""
        if not is_multi or not self.target_hz or self.target_hz <= 16:
            return
        if self.force_hz:
            _log.info(
                f"[DANGER] Multi-ring Hz safety cap bypassed "
                f"via force: {self.target_hz} Hz"
            )
        else:
            _log.info(
                f"[WARN] Multi-ring sessions are unstable above ~16 Hz due to hardware limitations. "
                f"Capping {self.target_hz} Hz -> 16.0 Hz."
            )
            self.target_hz = 16.0

    async def _start_single_ring(self) -> bool:
        """Single-ring connection and subscription flow."""
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
        bat = await self.connector.read_battery()
        state.battery = bat
        if bat is not None:
            if state.battery_start is None:
                state.battery_start = bat
            state.battery_end = bat
        await self._post_connect_setup(mac, state)
        ok = await self._subscribe_device_streams(mac)
        if not ok:
            state.status = "degraded"
        return True

    async def _discover_targets(
        self,
        scan_timeout: Optional[float],
        scan_attempts: Optional[int],
    ) -> Dict[str, Any]:
        """Discover matching rings, retrying once after a BT radio reset on Windows."""
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

        if not discovered and platform.system() == "Windows" and self.allow_reset_bt:
            _log.info(
                "[BT-RESET] No rings discovered and allow_reset_bt is enabled. "
                "Resetting Bluetooth adapter to clear stale connections..."
            )
            reset_ok = await self.connector._reset_bluetooth_radio()
            if reset_ok:
                _log.info("[BT-RESET] Rescanning after adapter reset...")
                discovered = await self.connector.discover_all_matching_rings(
                    include_device=True,
                    scan_timeout=s_timeout,
                    attempts=s_attempts,
                    retry_delay=0.5,
                    stop_if_found=True,
                )

        return {d["address"].upper(): d for d in discovered}

    async def _start_multi_devices(
        self,
        ring_addresses: Optional[List[str]],
        monitor_all: bool,
        max_devices: Optional[int],
        stagger_delay: float,
        scan_timeout: Optional[float],
        scan_attempts: Optional[int],
    ) -> bool:
        """Connect to multiple rings: discover when needed, then connect with stagger."""
        targets = [a.upper() for a in (ring_addresses or [])]
        discovered_by_mac: Dict[str, Any] = {}

        if monitor_all or not targets:
            discovered_by_mac = await self._discover_targets(
                scan_timeout, scan_attempts
            )
            if not targets:
                targets = list(discovered_by_mac.keys())

        if max_devices is not None:
            targets = targets[: max(0, max_devices)]

        return await self._connect_targets(targets, discovered_by_mac, stagger_delay)

    async def _connect_targets(
        self,
        targets: List[str],
        discovered_by_mac: Dict[str, Any],
        stagger_delay: float,
    ) -> bool:
        """Connect to each target with stagger; return True if any connected."""
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

        return connected_any

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
        """Start monitoring one or many rings."""
        self.start_time = datetime.now()
        self.running = True
        self.capture_armed = False
        self._auto_reconnect = auto_reconnect

        is_multi = monitor_all or len(ring_addresses or []) > 1
        self._apply_multi_ring_hz_cap(is_multi)

        if not ring_addresses and not monitor_all:
            started = await self._start_single_ring()
        else:
            started = await self._start_multi_devices(
                ring_addresses=ring_addresses,
                monitor_all=monitor_all,
                max_devices=max_devices,
                stagger_delay=stagger_delay,
                scan_timeout=scan_timeout,
                scan_attempts=scan_attempts,
            )

        if not started:
            self.running = False
            return False

        self.start_time = datetime.now()
        self.capture_armed = True

        self._health_task = asyncio.create_task(self._connection_health_loop())
        return True

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
                _log.debug("Health-loop error for %s", mac, exc_info=True)

            await asyncio.sleep(1.0)

    async def stop_multi(self) -> None:
        """Stop monitoring all rings and drain output buffers."""
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

        await self._drain_writers_and_report()
        self._write_session_manifest()

    stop_monitoring = stop_multi

    def _write_session_manifest(self) -> Optional[Path]:
        """Generate scientific session provenance manifest if logging is enabled."""
        if not self.enable_logging:
            return None
        session_dir = self.log_dir / f"SessionDate_{self.session_timestamp}"
        if not session_dir.exists():
            return None
        try:
            mode_label = (
                f"0x{self.initial_mode & 0xFF:02X} ({NuanicConnector.MODE_LABELS.get(self.initial_mode & 0xFF, 'unknown')})"
                if self.initial_mode is not None
                else "default"
            )
            config = {
                "target_hz": self.target_hz,
                "operational_mode": mode_label,
                "filter_enabled": bool(self.apply_filter),
                "csv_layout": self.csv_layout,
                "participant_id": self.participant_id,
            }
            manifest = generate_session_manifest(
                session_dir=session_dir,
                session_id=f"SessionDate_{self.session_timestamp}",
                start_time=self.start_time,
                end_time=datetime.now(),
                configuration=config,
                device_states=self.device_states,
            )
            manifest_path = session_dir / "session_manifest.json"
            _log.info("Session manifest generated: %s", manifest_path)
            return manifest_path
        except Exception as e:
            _log.error("Failed to generate session manifest: %s", e, exc_info=True)
            return None

    async def _drain_writers_and_report(self) -> None:
        """Drain all writer tasks and surface dropped-row warnings at session end."""
        for state in self.device_states.values():
            await drain_writer_tasks(
                [
                    state.writer_task,
                    state.stream_writer_task,
                    state.computed_writer_task,
                    state.imu_writer_task,
                ]
            )
            if state.dropped_rows > 0:
                _log.info(
                    f"[WARN] {state.mac}: {state.dropped_rows} log rows "
                    f"were dropped (queue full). Consider reducing target_hz "
                    f"or increasing queue size."
                )

    def dashboard_rows(self) -> List[Dict[str, str]]:
        """Return formatted status rows for TUI / terminal dashboard rendering."""
        rows: List[Dict[str, str]] = []
        for mac, state in self.device_states.items():
            bat_str = f"{state.battery}%" if state.battery else "-"
            eda_str = str(state.raw_eda) if state.raw_eda else "N/A"
            filt_str = f"{state.filtered_us:.3f}" if state.filtered_us else "N/A"
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
                    "dne_score": dne_str,
                    "observed_hz": rate_hz,
                    "rate_control": state.rate_control_status,
                    "imu_xyz": f"({imu_x}, {imu_y}, {imu_z})",
                }
            )
        return rows

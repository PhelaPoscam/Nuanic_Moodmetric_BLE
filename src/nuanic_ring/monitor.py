"""Real-time multi-ring monitor for Nuanic ring streams."""

import asyncio
import csv
import json
import logging
import math
import platform
import statistics
import struct
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .connector import NuanicConnector
from .signal_processing import SignalConditioner

_log = logging.getLogger(__name__)


@dataclass
class RingDeviceState:
    """Per-device runtime state to keep data pipelines isolated."""

    mac: str
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
        default_factory=lambda: deque(maxlen=2000)
    )
    imu_batch_buffer: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=500)
    )

    mm_filtered_us_wave: Deque[float] = field(
        default_factory=lambda: deque(maxlen=2000)
    )
    mm_arousal_wave: Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    live_dna_index: Deque[int] = field(default_factory=lambda: deque(maxlen=2000))
    live_dna_word2: Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    dne_stress_index_wave: Deque[float] = field(
        default_factory=lambda: deque(maxlen=2000)
    )

    imu_index: Deque[int] = field(default_factory=lambda: deque(maxlen=500))
    imu_intensity: Deque[float] = field(default_factory=lambda: deque(maxlen=500))

    # Independent processing chain per ring (lazy-init with actual observed Hz)
    signal_conditioner: Optional[SignalConditioner] = None

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
    imu_log_file: Optional[Path] = None
    imu_log_queue: Optional[asyncio.Queue[List[Any]]] = None
    imu_writer_task: Optional[asyncio.Task[None]] = None
    dropped_rows: int = 0
    marker_count: int = 0

    # Reconnect bookkeeping
    reconnect_attempt: int = 0
    last_seen: Optional[datetime] = None

    # Timestamp smoothing — per-stream anchors for hardware-clock reconstruction
    d306_ts_anchor: Optional[datetime] = None
    d306_clock_offset: Optional[int] = None
    imu_ts_anchor: Optional[datetime] = None
    imu_clock_offset: Optional[int] = None

    # Rate diagnostics and control status
    d306_observed_hz: float = 0.0
    imu_observed_hz: float = 0.0
    last_d306_ts: Optional[datetime] = None
    last_imu_ts: Optional[datetime] = None
    last_accepted_d306_ts: Optional[datetime] = None
    last_accepted_imu_ts: Optional[datetime] = None
    d306_intervals: Deque[float] = field(default_factory=lambda: deque(maxlen=128))
    imu_intervals: Deque[float] = field(default_factory=lambda: deque(maxlen=128))
    rate_control_status: str = "not-attempted"
    rate_control_detail: str = ""
    heartbeat_tick: bool = False


class NuanicMonitor:
    """Multi-device monitor with isolated per-device state and logging."""

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
        force_hz: bool = False,
        use_warmup: bool = False,
        warmup_delay: float = 3.0,
        allow_reset_bt: bool = False,
        participant_id: Optional[str] = None,
        apply_filter: bool = False,
        initial_mode: Optional[int] = None,
    ):
        self.log_dir = Path(log_dir)
        self.enable_logging = enable_logging
        if csv_layout not in {"combined", "split", "both", "nuanic"}:
            raise ValueError("csv_layout must be one of: combined, split, both, nuanic")
        self.csv_layout = csv_layout
        self.force_hz = force_hz
        self.participant_id = participant_id
        if self.enable_logging:
            self.log_dir.mkdir(parents=True, exist_ok=True)

        self.connector = NuanicConnector()
        self.imu_refresh_packets = max(1, imu_refresh_packets)
        self.clear_console = clear_console
        self.target_hz = target_hz
        self.equalize_mode = equalize_mode
        self.attempt_ring_rate_control = attempt_ring_rate_control
        self.use_warmup = use_warmup
        self.warmup_delay = warmup_delay
        self.allow_reset_bt = allow_reset_bt
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

    def _get_signal_conditioner(self, state: RingDeviceState) -> SignalConditioner:
        """Return the per-device signal conditioner, creating it lazily with the observed Hz.

        The conditioner is tuned once at the first packet's observed Hz (falling
        back to 8 Hz before the first interval is measured) and is never retuned
        mid-session. Only affects the optional ``--filter`` path.
        """
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
        """Reconstruct a millisecond-accurate PC timestamp from the ring's hardware clock.

        Anchors the first packet's clock value to ``datetime.now()``, then derives
        every subsequent timestamp as ``anchor + (clock − offset)`` ms. This eliminates
        duplicate timestamps caused by BLE packet bursting / coarse Windows clock
        ticks. D306 and IMU have independent counters, so each stream gets its own
        anchor.
        """
        if stream == "d306":
            anchor = state.d306_ts_anchor
            offset = state.d306_clock_offset
        else:
            anchor = state.imu_ts_anchor
            offset = state.imu_clock_offset

        if anchor is None or offset is None or clock < offset:
            anchor = datetime.now()
            offset = clock
            if stream == "d306":
                state.d306_ts_anchor = anchor
                state.d306_clock_offset = offset
            else:
                state.imu_ts_anchor = anchor
                state.imu_clock_offset = offset

        elapsed_ms = (clock - offset) & 0xFFFFFFFF
        smoothed_ts = anchor + timedelta(milliseconds=elapsed_ms)

        if self.start_time:
            elapsed_session_ms = int(
                (smoothed_ts - self.start_time).total_seconds() * 1000
            )
        else:
            elapsed_session_ms = elapsed_ms

        return smoothed_ts, max(1, elapsed_session_ms)

    @staticmethod
    def _nuanic_ts_fields(ts: datetime) -> Tuple[str, str]:
        """Return (unix_str, utc_iso_str) for the Nuanic CSV export layout."""
        from datetime import timezone

        unix_str = f"{ts.timestamp():.6f}"
        utc_str = (
            ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "+00"
        )
        return unix_str, utc_str

    def _parse_d306_packet(self, data: bytes) -> Optional[Dict[str, Any]]:
        if len(data) != 16:
            return None

        timestamp_ms, instant, dne = struct.unpack("<Qii", data)
        return {
            "timestamp_ms": timestamp_ms,
            "instant": instant,
            "dne": dne,
            # backward compatibility keys for existing code:
            "clock": timestamp_ms & 0xFFFFFFFF,
            "context": instant,
            "eda_value": instant,
            "dne_stress_index": dne,
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
        if len(magnitudes) > 1:
            motion_intensity = statistics.stdev(magnitudes)
        else:
            motion_intensity = 0.0

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

        state = RingDeviceState(mac=mac_key)
        self.device_states[mac_key] = state

        if self.enable_logging:
            if self.csv_layout in {"combined", "both", "nuanic"}:
                state.log_queue = asyncio.Queue(maxsize=5000)
            if self.csv_layout in {"split", "both"}:
                state.stream_log_queue = asyncio.Queue(maxsize=5000)
                state.computed_log_queue = asyncio.Queue(maxsize=5000)
            state.imu_log_queue = asyncio.Queue(maxsize=5000)
            # Eagerly create log files and writer tasks here so the BLE notify
            # callback hot path never performs blocking filesystem I/O (open/mkdir).
            # Only when a session is running: the writer loop exits immediately
            # if `running` is False, so we must not start it pre-session.
            if self.running:
                self._initialize_log_file(state)
                self._initialize_split_log_files(state)
                self._initialize_imu_log_file(state)

        return state

    def _log_filename(self, state: RingDeviceState, suffix: str = "") -> str:
        safe_mac = state.mac.replace(":", "-")
        parts = []
        if self.participant_id:
            parts.append(self.participant_id)
        parts.append(f"ring-{safe_mac[-6:]}")
        if suffix:
            parts.append(suffix)
        return "_".join(parts) + ".csv"

    def _open_log_file(
        self, state: RingDeviceState, suffix: str, header: List[str]
    ) -> Path | None:
        """Create a CSV log file with header row, return its path or None."""
        if not self.enable_logging:
            return None
        filename = self._log_filename(state, suffix)
        session_folder = self.log_dir / f"SessionDate_{self.session_timestamp}" / "csvs"
        session_folder.mkdir(parents=True, exist_ok=True)
        file_path = session_folder / filename
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)
            _log.info("Started log for %s: %s", state.mac, filename)
            return file_path
        except Exception as e:
            _log.error("Error initializing log for %s: %s", state.mac, e)
            return None

    def _start_writer(
        self, state: RingDeviceState, queue: asyncio.Queue[List[Any]], file_path: Path
    ) -> Optional[asyncio.Task[None]]:
        writer = self._csv_writer_loop(state, queue, file_path)
        try:
            return asyncio.create_task(writer)
        except RuntimeError:
            # No running event loop (e.g. sync callers). The writer loop will be
            # started by a later async call into _initialize_*.
            writer.close()
            return None

    def _initialize_log_file(self, state: RingDeviceState) -> None:
        """Initialize the CSV log file and its writer task (eagerly on state creation)."""
        if not state.log_queue:
            return
        if state.log_file and state.writer_task:
            return
        if self.csv_layout == "nuanic":
            header = ["address", "time_unix", "time", "dne", "srl", "srrn", "eda"]
        else:
            header = [
                "timestamp",
                "elapsed_ms",
                "device_mac",
                "connection_state",
                "data_type",
                "EDA_Raw_Value",
                "Stress_Index",
                "D306_Clock",
                "D306_Context",
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
        if not state.log_file:
            state.log_file = self._open_log_file(state, "", header)
        if state.log_file and state.log_queue:
            state.writer_task = self._start_writer(
                state, state.log_queue, state.log_file
            )

    def _initialize_split_log_files(self, state: RingDeviceState) -> None:
        """Initialize raw-stream and computed CSV files and their writer tasks."""
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
            "Skin_Resistance_kOhm",
            "Skin_Conductance_uS",
            "MM_Filtered_uS",
            "SCR_Frequency_Per_Min",
            "SCR_Amplitude",
            "MM_Arousal_Score",
            "MM_Calibrated",
            "D306_Observed_Hz",
            "IMU_Observed_Hz",
            "Rate_Target_Hz",
            "Rate_Control_Status",
            "Equalize_Mode",
            "Equalize_WouldDrop",
            "marker_label",
            "marker_source",
        ]
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
        """Initialize the dedicated IMU CSV file and its writer task."""
        if not state.imu_log_queue:
            return
        if state.imu_log_file and state.imu_writer_task:
            return
        header = [
            "timestamp",
            "elapsed_ms",
            "clock",
            "context",
            "motion_intensity",
            "x",
            "y",
            "z",
            "marker",
        ]
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

            try:
                with open(
                    log_file,
                    "a",
                    newline="",
                    encoding="utf-8",
                ) as file:
                    writer = csv.writer(file)
                    writer.writerows(batch)
            except Exception:
                _log.debug("CSV write error for %s", log_file, exc_info=True)
            batch.clear()

    def _enqueue_to(
        self,
        state: RingDeviceState,
        queue: Optional[asyncio.Queue[List[Any]]],
        row: List[Any],
        file_ready: bool,
        initializer: Callable[[RingDeviceState], None],
    ) -> None:
        """Guard, lazy-init, and enqueue a CSV row; count drops on full queue."""
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

        # Columns 5-12 of combined log, ending with JSON marker payload
        marker_fields = [
            "",  # EDA_Raw_Value
            "",  # Stress_Index
            "",  # D306_Clock
            "",  # D306_Context
            "",  # State_Code
            "",  # payload_hex
            "",  # full_packet_hex
            marker_payload,  # decoded_fields
        ]

        inserted = 0
        for state in self.device_states.values():
            if self.csv_layout != "nuanic":
                row = (
                    self._base_row(state, "MARKER")
                    + marker_fields
                    + self._row_rate_tail(state, would_drop=False)
                )
                self._enqueue_log(state, row)

            # Split stream log: 8 data cols + marker_label + marker_source
            stream_row = self._base_row(state, "MARKER") + [
                "",  # D306_Clock
                "",  # D306_Context
                "",  # EDA_Raw_Value
                "",  # Stress_Index
                "",  # State_Code
                "",  # payload_hex
                "",  # full_packet_hex
                "",  # decoded_fields
                clean_label,
                source,
            ]
            # Split computed log: 15 data cols + marker_label + marker_source
            computed_row = self._base_row(state, "MARKER") + [
                "",  # Source_D306_Clock
                "",  # Source_D306_Context
                "",  # Skin_Resistance_kOhm
                "",  # Skin_Conductance_uS
                "",  # MM_Filtered_uS
                "",  # SCR_Frequency_Per_Min
                "",  # SCR_Amplitude
                "",  # MM_Arousal_Score
                "",  # MM_Calibrated
                "",  # D306_Observed_Hz
                "",  # IMU_Observed_Hz
                "",  # Rate_Target_Hz
                "",  # Rate_Control_Status
                "",  # Equalize_Mode
                "",  # Equalize_WouldDrop
                clean_label,
                source,
            ]
            self._enqueue_stream_log(state, stream_row)
            self._enqueue_computed_log(state, computed_row)

            imu_marker_row = [
                datetime.now().isoformat(timespec="milliseconds"),
                int(self._elapsed_seconds() * 1000),
                "",  # clock
                "",  # context
                "",  # motion_intensity
                "",  # x
                "",  # y
                "",  # z
                marker_payload,
            ]
            self._enqueue_imu_log(state, imu_marker_row)

            state.marker_count += 1
            inserted += 1

        return inserted

    # ponytail: "stress" callback subscribes to LIVE_DNE_UUID (d306262b) —
    # the preprocessed Instant Indicator + DNE stream.  The actual raw EDA
    # stream (42dcb71b) arrives via _make_live_eda_callback, and the state
    # indicator (3c180fcc) via the confusingly-named _make_raw_eda_callback.
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
                _row_kw: Dict[str, Any] = {
                    "custom_ts": smoothed_ts,
                    "custom_elapsed": elapsed_ms,
                }

                if self.csv_layout == "nuanic":
                    ts_unix, ts_str = self._nuanic_ts_fields(smoothed_ts)

                    # Compute SRL (Tonic Resistance in Ohms) from our Filtered Conductance
                    srl_ohms = int(1_000_000 / filtered_us) if filtered_us > 0 else 0
                    # SRRN (Skin Resistance Reactions N) is our freq (SCRs per minute)
                    srrn = f"{freq:.1f}"

                    row = [
                        state.mac,
                        ts_unix,
                        ts_str,
                        dne_stress_index,
                        srl_ohms,
                        srrn,
                        eda_value,
                    ]
                    self._enqueue_log(state, row)
                else:
                    row = (
                        self._base_row(state, "D306_EDA", **_row_kw)
                        + [
                            eda_value,
                            dne_stress_index,
                            clock,
                            context,
                            "",
                            data.hex(),
                            data.hex(),
                            "",
                        ]
                        + self._row_rate_tail(state, would_drop)
                    )
                    self._enqueue_log(state, row)

                stream_row = self._base_row(state, "D306_EDA", **_row_kw) + [
                    clock,
                    context,
                    eda_value,
                    dne_stress_index,
                    "",
                    data.hex(),
                    data.hex(),
                    "",
                    "",
                    "",
                ]
                computed_row = (
                    self._base_row(state, "D306_EDA_COMPUTED", **_row_kw)
                    + [
                        clock,
                        context,
                        f"{resistance_kohm:.4f}",
                        f"{conductance_us:.4f}",
                        f"{filtered_us:.4f}",
                        f"{freq:.4f}",
                        f"{amp:.4f}",
                        f"{state.arousal_score:.2f}",
                        "1",
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
                _log.debug("Stress callback error for %s", mac, exc_info=True)

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
                state.imu_index.append(state.imu_batch_count)
                state.imu_intensity.append(parsed_batch["motion_intensity"])

                smoothed_ts, elapsed_ms = self._get_smoothed_time(
                    state, "imu", parsed_batch["clock"]
                )

                timestamp_iso = smoothed_ts.isoformat(timespec="milliseconds")
                for x, y, z in parsed_batch["samples"]:
                    imu_row = [
                        timestamp_iso,
                        elapsed_ms,
                        parsed_batch["clock"],
                        parsed_batch["context"],
                        f"{parsed_batch['motion_intensity']:.4f}",
                        x,
                        y,
                        z,
                        "",
                    ]
                    self._enqueue_imu_log(state, imu_row)

            except Exception:
                _log.debug("IMU callback error for %s", mac, exc_info=True)

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

                if self.csv_layout != "nuanic":
                    row = (
                        self._base_row(state, "STATE_3C18")
                        + [
                            "",  # 5: EDA_Raw_Value
                            "",  # 6: Stress_Index
                            "",  # 12: D306_Clock
                            "",  # 13: D306_Context
                            (
                                state_code if state_code is not None else ""
                            ),  # 14: State_Code
                            data.hex(),  # 15: payload_hex
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
                    state_code if state_code is not None else "",
                    data.hex(),
                    data.hex(),
                    "",
                    "",
                    "",
                ]
                self._enqueue_stream_log(state, stream_row)
            except Exception:
                _log.debug("Raw EDA callback error for %s", mac, exc_info=True)

        return _cb

    def _make_live_eda_callback(self, mac: str):
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
                    boot_count, timestamp_ms, eda_ohm = struct.unpack("<HQI", data)
                    res_kohm = eda_ohm / 1000.0
                    cond_us = (1000000.0 / eda_ohm) if eda_ohm > 0 else 0.0
                    decoded = {
                        "boot_count": boot_count,
                        "timestamp_ms": timestamp_ms,
                        "eda_ohm": eda_ohm,
                        "resistance_kohm": round(res_kohm, 3),
                        "conductance_us": round(cond_us, 3),
                    }
                    eda_str = str(eda_ohm)

                    # Update state telemetry for Mode 1 (MODE_RAW_EDA / 42dc)
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
                    row = [
                        state.mac,
                        ts_unix,
                        ts_str,
                        0,  # dne is 0 in mode 1
                        srl_ohms,
                        srrn,
                        eda_val,
                    ]
                    self._enqueue_log(state, row)
                else:
                    row = (
                        self._base_row(state, "LIVE_EDA_42DC")
                        + [
                            eda_str,  # 5: EDA_Raw_Value (Ohms)
                            "0",  # 6: Stress_Index
                            str(timestamp_ms),  # 12: D306_Clock
                            "",  # 13: D306_Context
                            "",  # 14: State_Code
                            data.hex(),  # 15: payload_hex
                            data.hex(),  # 22: full_packet_hex
                            json.dumps(decoded),  # 23: decoded_fields
                        ]
                        + self._row_rate_tail(state, would_drop)
                    )
                    self._enqueue_log(state, row)

                    stream_row = self._base_row(state, "LIVE_EDA_42DC") + [
                        str(timestamp_ms),
                        "",
                        eda_str,
                        "0",
                        "",
                        data.hex(),
                        data.hex(),
                        json.dumps(decoded),
                        "",
                        "",
                    ]
                    computed_row = (
                        self._base_row(state, "LIVE_EDA_COMPUTED")
                        + [
                            str(timestamp_ms),
                            "",
                            f"{res_kohm:.4f}" if len(data) == 14 else "",
                            f"{cond_us:.4f}" if len(data) == 14 else "",
                            f"{filtered_us:.4f}" if len(data) == 14 else "",
                            f"{freq:.4f}" if len(data) == 14 else "",
                            f"{amp:.4f}" if len(data) == 14 else "",
                            f"{state.arousal_score:.2f}" if len(data) == 14 else "",
                            "1",
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
                _log.debug("Live EDA callback error for %s", mac, exc_info=True)

        return _cb

    async def _subscribe_device_streams(self, mac: str) -> bool:
        results = await asyncio.gather(
            self.connector.subscribe_to_imu(self._make_imu_callback(mac), address=mac),
            self.connector.subscribe_to_stress(
                self._make_stress_callback(mac), address=mac
            ),
            self.connector.subscribe_to_raw_eda(
                self._make_raw_eda_callback(mac), address=mac
            ),
            self.connector.subscribe_to_live_eda(
                self._make_live_eda_callback(mac), address=mac
            ),
        )
        return all(results)

    async def _unsubscribe_device_streams(self, mac: str) -> None:
        await asyncio.gather(
            self.connector.unsubscribe_from_imu(address=mac),
            self.connector.unsubscribe_from_stress(address=mac),
            self.connector.unsubscribe_from_raw_eda(address=mac),
            self.connector.unsubscribe_from_live_eda(address=mac),
        )

    async def _warmup_sequence(self, mac: str, device: Any = None) -> None:
        """Optional firmware warmup: prime the ring at the target rate, then release."""
        if not (self.target_hz and self.attempt_ring_rate_control and self.use_warmup):
            return
        _log.info(
            f"[WARMUP] Priming firmware for Rate Control ({self.target_hz}Hz) on {mac}..."
        )
        warm_ok = await self.connector.connect_device(address=mac, device=device)
        if warm_ok:
            # Set the rate to kick the ring into gear, then disconnect
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

        # Aggressive connection fallback if the OS link state is stuck
        if not ok and self.allow_reset_bt:
            _log.info(
                f"[RECOVERY] Connection failed for {mac}. Trying aggressive BT radio reset..."
            )
            await self.connector._reset_bluetooth_radio()
            await asyncio.sleep(1.0)
            ok = await self.connector.connect_device(address=mac, device=device)
        elif not ok:
            _log.info(
                f"[RECOVERY] Connection failed for {mac}. (Aggressive reset disabled)"
            )

        if not ok:
            state.status = "disconnected"
            return False

        state.status = "connected"
        state.reconnect_attempt = 0
        state.battery = await self.connector.read_battery(address=mac)

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
        """Backward-compatible single-ring selection flow."""
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

    async def _drain_writers_and_report(self) -> None:
        """Drain all writer tasks and surface dropped-row warnings at session end."""
        for state in self.device_states.values():
            for task in (
                state.writer_task,
                state.stream_writer_task,
                state.computed_writer_task,
                state.imu_writer_task,
            ):
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            if state.dropped_rows > 0:
                _log.info(
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

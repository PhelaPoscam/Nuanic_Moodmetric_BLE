"""Per-device telemetry state definitions and buffers for Nuanic Ring."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from nuanic_ring.dsp.signal_processing import SignalConditioner


@dataclass
class RingDeviceState:
    """Per-device runtime state to keep data pipelines isolated."""

    mac: str
    status: str = "disconnected"
    battery: Optional[int] = None
    battery_start: Optional[int] = None
    battery_end: Optional[int] = None

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

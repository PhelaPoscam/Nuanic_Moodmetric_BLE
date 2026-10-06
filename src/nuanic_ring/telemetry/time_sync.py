"""Hardware clock reconstruction and timestamp smoothing for Nuanic ring streams."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from nuanic_ring.telemetry.device_state import RingDeviceState


def get_smoothed_time(
    state: RingDeviceState,
    stream: str,
    clock: int,
    start_time: Optional[datetime] = None,
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

    if start_time:
        elapsed_session_ms = int((smoothed_ts - start_time).total_seconds() * 1000)
    else:
        elapsed_session_ms = elapsed_ms

    return smoothed_ts, max(1, elapsed_session_ms)


def nuanic_ts_fields(ts: datetime) -> Tuple[str, str]:
    """Return (unix_str, utc_iso_str) for the Nuanic CSV export layout."""
    unix_str = f"{ts.timestamp():.6f}"
    utc_str = ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "+00"
    return unix_str, utc_str

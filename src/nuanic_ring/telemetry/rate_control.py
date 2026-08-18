"""Sample-rate calculation, intervals tracking, and equalization logic."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from nuanic_ring.telemetry.device_state import RingDeviceState


def update_observed_hz(
    state: RingDeviceState,
    stream_name: str,
    now: datetime,
) -> None:
    """Calculate moving average observed Hz for D306 or IMU stream."""
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


def equalize_decision(
    state: RingDeviceState,
    stream_name: str,
    target_hz: Optional[float],
    equalize_mode: str = "off",
    now: Optional[datetime] = None,
) -> bool:
    """Determine whether an incoming packet should be dropped to achieve target rate."""
    if equalize_mode == "off" or not target_hz:
        return False

    target_dt = 1.0 / max(1e-6, target_hz)
    last_ts = (
        state.last_accepted_d306_ts
        if stream_name == "d306"
        else state.last_accepted_imu_ts
    )
    if last_ts is None:
        return False

    current_now = now or datetime.now()
    current_dt = (current_now - last_ts).total_seconds()
    should_drop = current_dt < target_dt

    return should_drop


def build_row_rate_tail(
    state: RingDeviceState,
    target_hz: Optional[float],
    equalize_mode: str,
    would_drop: bool,
) -> List[str]:
    """Format rate control diagnostics columns."""
    return [
        (f"{state.d306_observed_hz:.3f}" if state.d306_observed_hz > 0 else ""),
        (f"{state.imu_observed_hz:.3f}" if state.imu_observed_hz > 0 else ""),
        f"{target_hz:.2f}" if target_hz else "",
        state.rate_control_status,
        equalize_mode,
        "1" if would_drop else "0",
    ]

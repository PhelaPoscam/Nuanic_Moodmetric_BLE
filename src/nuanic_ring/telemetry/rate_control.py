"""Sample-rate calculation, intervals tracking, and equalization logic."""

from __future__ import annotations

from datetime import datetime
from statistics import fmean
from typing import Dict, Optional

from nuanic_ring.telemetry.device_state import RingDeviceState


def update_observed_hz(
    state: RingDeviceState,
    stream_name: str,
    now: datetime,
) -> None:
    """Calculate moving average observed Hz for D306 or IMU stream."""
    stream = "d306" if stream_name == "d306" else "imu"
    last = getattr(state, f"last_{stream}_ts")
    if last is not None:
        dt = (now - last).total_seconds()
        if dt > 0:
            intervals = getattr(state, f"{stream}_intervals")
            intervals.append(dt)
            mean_dt = fmean(intervals)
            if mean_dt > 0:
                setattr(state, f"{stream}_observed_hz", 1.0 / mean_dt)
    setattr(state, f"last_{stream}_ts", now)


def equalize_decision(
    state: RingDeviceState,
    stream_name: str,
    target_hz: Optional[float],
    equalize_mode: str = "off",
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

    return (datetime.now() - last_ts).total_seconds() < target_dt


def build_row_rate_tail(
    state: RingDeviceState,
    target_hz: Optional[float],
    equalize_mode: str,
    would_drop: bool,
) -> Dict[str, str]:
    """Rate-control diagnostics columns, keyed by log-row field name."""
    return {
        "D306_Observed_Hz": (
            f"{state.d306_observed_hz:.3f}" if state.d306_observed_hz > 0 else ""
        ),
        "IMU_Observed_Hz": (
            f"{state.imu_observed_hz:.3f}" if state.imu_observed_hz > 0 else ""
        ),
        "Rate_Target_Hz": f"{target_hz:.2f}" if target_hz else "",
        "Rate_Control_Status": state.rate_control_status,
        "Equalize_Mode": equalize_mode,
        "Equalize_WouldDrop": "1" if would_drop else "0",
    }

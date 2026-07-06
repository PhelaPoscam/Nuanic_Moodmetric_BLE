"""Moodmetric-compatible helpers for Nuanic ring packets and scoring.

This module provides a calibration-based 1-100 scorer inspired by
Moodmetric-style aggregation. The exact proprietary Moodmetric formula is
not public — the score here is an interpretable approximation.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta


class MMLikeScorer:
    """Calibration-based 1-100 scorer using SCR frequency/amplitude and SCL."""

    def __init__(
        self,
        calibration_seconds: int = 120,
        weights: tuple[float, float, float] = (0.4, 0.3, 0.3),
        freq_ref: float = 10.0,
        amp_ref: float = 10.0,
        scl_ref_us: float = 8.0,
    ):
        self.calibration_seconds = max(10, int(calibration_seconds))
        self.weights = weights
        self.freq_ref = max(1e-6, freq_ref)
        self.amp_ref = max(1e-6, amp_ref)
        self.scl_ref_us = max(1e-6, scl_ref_us)

        self.started_at: datetime | None = None
        self.calibration_min: float | None = None
        self.calibration_max: float | None = None
        self.latest_raw_score: float | None = None
        self.latest_scaled_score: float | None = None

        self._event_times: deque[datetime] = deque()
        self._last_event_time: datetime | None = None
        self._baseline_ema: float | None = None

    def _raw_score(self, freq: float, amp: float, scl: float) -> float:
        w_freq, w_amp, w_scl = self.weights
        freq_n = max(
            0.0, min(1.0, math.log1p(max(0.0, freq)) / math.log1p(self.freq_ref))
        )
        amp_n = max(0.0, min(1.0, math.log1p(max(0.0, amp)) / math.log1p(self.amp_ref)))
        scl_n = max(
            0.0, min(1.0, math.log1p(max(0.0, scl)) / math.log1p(self.scl_ref_us))
        )
        return max(0.0, min(1.0, (w_freq * freq_n) + (w_amp * amp_n) + (w_scl * scl_n)))

    def update_scr_features(
        self,
        tonic_value: float,
        now: datetime | None = None,
        trigger_threshold: float = 0.02,
        min_event_gap_seconds: float = 3.0,
    ) -> tuple[float, float]:
        """Update internal SCR event detector from a tonic-like signal.

        Returns:
            (scr_frequency_per_min, scr_amplitude)
        """
        if now is None:
            now = datetime.now()

        if self._baseline_ema is None:
            self._baseline_ema = tonic_value
        else:
            self._baseline_ema = (0.95 * self._baseline_ema) + (0.05 * tonic_value)

        scr_amp = max(0.0, tonic_value - self._baseline_ema)

        event_allowed = (
            self._last_event_time is None
            or (now - self._last_event_time).total_seconds() >= min_event_gap_seconds
        )
        if scr_amp >= trigger_threshold and event_allowed:
            self._event_times.append(now)
            self._last_event_time = now

        minute_ago = now - timedelta(seconds=60)
        while self._event_times and self._event_times[0] < minute_ago:
            self._event_times.popleft()

        return float(len(self._event_times)), scr_amp

    def is_calibrated(self, now: datetime | None = None) -> bool:
        if now is None:
            now = datetime.now()
        if (
            self.started_at is None
            or self.calibration_min is None
            or self.calibration_max is None
        ):
            return False
        elapsed = (now - self.started_at).total_seconds()
        return (
            elapsed >= self.calibration_seconds
            and (self.calibration_max - self.calibration_min) > 1e-6
        )

    def update(
        self,
        scr_frequency_per_min: float,
        scr_amplitude: float,
        scl_microsiemens: float,
        now: datetime | None = None,
    ) -> dict[str, float | bool]:
        """Update scorer with new features and return score state."""
        if now is None:
            now = datetime.now()
        if self.started_at is None:
            self.started_at = now

        raw = self._raw_score(scr_frequency_per_min, scr_amplitude, scl_microsiemens)
        self.latest_raw_score = raw

        if self.calibration_min is None or raw < self.calibration_min:
            self.calibration_min = raw
        if self.calibration_max is None or raw > self.calibration_max:
            self.calibration_max = raw

        if self.is_calibrated(now):
            span = self.calibration_max - self.calibration_min
            scaled = 1.0 + 99.0 * max(
                0.0, min(1.0, (raw - self.calibration_min) / span)
            )
            self.latest_scaled_score = scaled
        else:
            self.latest_scaled_score = None

        elapsed = (now - self.started_at).total_seconds() if self.started_at else 0.0
        remaining = max(0.0, self.calibration_seconds - elapsed)

        return {
            "raw_score_0_to_1": raw,
            "mm_like_1_to_100": (
                self.latest_scaled_score
                if self.latest_scaled_score is not None
                else 0.0
            ),
            "calibrated": self.latest_scaled_score is not None,
            "calibration_seconds_remaining": remaining,
            "calibration_min": (
                self.calibration_min if self.calibration_min is not None else 0.0
            ),
            "calibration_max": (
                self.calibration_max if self.calibration_max is not None else 0.0
            ),
        }

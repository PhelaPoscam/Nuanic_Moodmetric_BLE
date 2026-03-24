"""Moodmetric-compatible helpers for Nuanic ring packets and scoring.

This module provides:
- Decoding helpers for 7-byte streaming packets and 2-byte raw-resistance packets.
- A calibration-based 1-100 score inspired by Moodmetric-style aggregation.

The exact proprietary Moodmetric formula is not public. The score here is an
interpretable approximation that combines SCR-like frequency, SCR amplitude,
and SCL (skin conductance) and scales the weighted result to a personal
1-100 range using per-user min/max calibration.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import math

# Empirical linear conversion from a field example:
# raw=3464 -> 845716.029603 ohms.
DEFAULT_OHMS_PER_RAW_UNIT = 244.14435034728638


def clamp(value: float, low: float, high: float) -> float:
    """Clamp value between low and high."""
    return max(low, min(high, value))


def decode_streaming_packet(packet: bytes) -> dict | None:
    """Decode 7-byte streaming packet.

    Layout (byte offsets):
    - 0: status bits
    - 1: MM-like number (0-255)
    - 2-3: instant EDA (uint16, big-endian)
    - 4-6: acceleration x/y/z (uint8)
    """
    if len(packet) < 7:
        return None

    status = packet[0]
    mm_number = packet[1]
    instant_eda = (packet[2] << 8) | packet[3]
    ax_raw, ay_raw, az_raw = packet[4], packet[5], packet[6]

    # Map 0..255 to 0..1 g (legacy-compatible interpretation).
    ax_g = ax_raw / 255.0
    ay_g = ay_raw / 255.0
    az_g = az_raw / 255.0
    accel_magnitude_g = math.sqrt(ax_g * ax_g + ay_g * ay_g + az_g * az_g)

    return {
        "status_bits_byte": status,
        "status_bits": {
            "MM_not_ready": (status >> 0) & 1,
            "ring_in_finger": (status >> 1) & 1,
            "battery_is_out": (status >> 2) & 1,
            "any_reaction": (status >> 3) & 1,
            "strong_reaction": (status >> 4) & 1,
            "MM_notification": (status >> 5) & 1,
            "relax_notification": (status >> 6) & 1,
            "reserved": (status >> 7) & 1,
        },
        "mm_number": mm_number,
        "instant_eda": instant_eda,
        "ax_raw": ax_raw,
        "ay_raw": ay_raw,
        "az_raw": az_raw,
        "ax_g": ax_g,
        "ay_g": ay_g,
        "az_g": az_g,
        "accel_magnitude_g": accel_magnitude_g,
    }


def decode_raw_resistance_packet(
    packet: bytes,
    ohms_per_raw_unit: float = DEFAULT_OHMS_PER_RAW_UNIT,
) -> dict | None:
    """Decode 2-byte raw-resistance packet and derive Ohms/Siemens values."""
    if len(packet) < 2:
        return None

    raw_value = (packet[0] << 8) | packet[1]
    skin_resistance_ohms = raw_value * ohms_per_raw_unit
    skin_conductance_siemens = (
        1.0 / skin_resistance_ohms if skin_resistance_ohms > 0 else 0.0
    )
    skin_conductance_microsiemens = skin_conductance_siemens * 1_000_000.0

    return {
        "raw_skin_resistance_value": raw_value,
        "skin_resistance_ohms": skin_resistance_ohms,
        "skin_conductance_siemens": skin_conductance_siemens,
        "skin_conductance_microsiemens": skin_conductance_microsiemens,
    }


@dataclass
class MMFeatures:
    """Features used for mm-like scoring."""

    scr_frequency_per_min: float
    scr_amplitude: float
    scl_microsiemens: float


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

        self._event_times = deque()
        self._last_event_time: datetime | None = None
        self._baseline_ema: float | None = None

    def _normalize_log(self, value: float, reference: float) -> float:
        value = max(0.0, value)
        return clamp(math.log1p(value) / math.log1p(reference), 0.0, 1.0)

    def _raw_score(self, f: MMFeatures) -> float:
        w_freq, w_amp, w_scl = self.weights
        freq_n = self._normalize_log(f.scr_frequency_per_min, self.freq_ref)
        amp_n = self._normalize_log(f.scr_amplitude, self.amp_ref)
        scl_n = self._normalize_log(f.scl_microsiemens, self.scl_ref_us)
        return clamp((w_freq * freq_n) + (w_amp * amp_n) + (w_scl * scl_n), 0.0, 1.0)

    def update_scr_features(
        self,
        tonic_value: float,
        now: datetime | None = None,
        trigger_threshold: float = 1.0,
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
        features: MMFeatures,
        now: datetime | None = None,
    ) -> dict[str, float | bool]:
        """Update scorer with new features and return score state."""
        if now is None:
            now = datetime.now()
        if self.started_at is None:
            self.started_at = now

        raw = self._raw_score(features)
        self.latest_raw_score = raw

        if self.calibration_min is None or raw < self.calibration_min:
            self.calibration_min = raw
        if self.calibration_max is None or raw > self.calibration_max:
            self.calibration_max = raw

        if self.is_calibrated(now):
            span = self.calibration_max - self.calibration_min
            scaled = 1.0 + 99.0 * clamp((raw - self.calibration_min) / span, 0.0, 1.0)
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

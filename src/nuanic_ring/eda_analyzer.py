"""EDA (Electrodermal Activity) analysis and interpretation"""

import struct
from typing import List, Dict, Tuple
from datetime import datetime


class NuanicEDAAnalyzer:
    """Analyzes electrodermal activity data from Nuanic ring"""

    # Constants for EDA interpretation
    MIN_PEAK_HEIGHT = 0.1  # Minimum rise amplitude for peak detection (µS)
    MIN_PEAK_DURATION = 0.5  # Minimum duration for peak detection (seconds)
    BASELINE_WINDOW = 60  # Seconds for baseline calculation

    def __init__(self):
        self.baseline = None
        self.eda_history = []
        self.peaks = []
        self.last_update = None

    def parse_eda_packet(self, eda_bytes: bytes) -> Dict:
        """Parse 77-byte EDA packet from 92-byte Nuanic packet

        The 77 bytes (bytes 15-91 of full packet) contain:
        - Possibly multiple EDA channels
        - Sampled at different rates than stress (1Hz stress vs potentially 16Hz EDA)

        Returns:
            dict with parsed EDA values and metadata
        """
        if len(eda_bytes) < 77:
            return None

        result = {
            "timestamp": datetime.now(),
            "raw_bytes": eda_bytes.hex(),
            "length": len(eda_bytes),
            "channels": [],
        }

        # Attempt to parse as potential EDA channels (assuming 1-4 channels)
        # Each channel might be 2-4 bytes depending on resolution

        # Try parsing as 4 channels of 16-bit values (most common for EDA)
        try:
            channel_count = 4
            bytes_per_channel = 4  # 2-byte value + metadata

            for i in range(channel_count):
                offset = i * bytes_per_channel
                if offset + 2 <= len(eda_bytes):
                    # Little-endian 16-bit value
                    value = struct.unpack("<H", eda_bytes[offset : offset + 2])[0]
                    result["channels"].append(
                        {
                            "channel": i,
                            "value": value,
                            "value_scaled": value
                            / 1024.0,  # Normalize to 0-1 or 0-63.5
                        }
                    )
        except:
            # If parsing fails, just return raw data
            pass

        return result

    def update_baseline(self, eda_value: float):
        """Update running baseline for EDA"""
        if self.baseline is None:
            self.baseline = eda_value
        else:
            # Exponential moving average
            alpha = 0.02  # Slow baseline drift
            self.baseline = alpha * eda_value + (1 - alpha) * self.baseline

    def detect_peak(self, eda_value: float) -> bool:
        """Detect if current reading is part of a peak response"""
        if self.baseline is None or len(self.eda_history) < 2:
            return False

        current_rise = eda_value - self.baseline
        recent_rise = self.eda_history[-1] - self.baseline if self.eda_history else 0

        # Peak detected if:
        # - Value is above baseline + threshold
        # - Value is increasing or plateau
        is_peak = (
            current_rise > self.MIN_PEAK_HEIGHT and current_rise >= recent_rise * 0.8
        )

        return is_peak

    def add_reading(self, eda_raw_value: float) -> Dict:
        """Add new EDA reading and analyze

        Args:
            eda_raw_value: Raw EDA value (typically 0-4095 for 12-bit ADC)

        Returns:
            dict: Analysis results
        """
        self.eda_history.append(eda_raw_value)
        self.last_update = datetime.now()

        # Keep only last N readings (1 minute at 1 Hz = 60 readings)
        if len(self.eda_history) > 60:
            self.eda_history.pop(0)

        # Update baseline
        self.update_baseline(eda_raw_value)

        # Detect peak
        is_peak = self.detect_peak(eda_raw_value)

        # Calculate statistics
        stats = {
            "current_value": eda_raw_value,
            "baseline": self.baseline,
            "tonic": self.baseline,  # Baseline component
            "phasic": eda_raw_value - self.baseline,  # Dynamic component
            "is_peak": is_peak,
            "history_length": len(self.eda_history),
            "min_recent": (
                min(self.eda_history[-10:])
                if len(self.eda_history) >= 10
                else min(self.eda_history)
            ),
            "max_recent": (
                max(self.eda_history[-10:])
                if len(self.eda_history) >= 10
                else max(self.eda_history)
            ),
            "avg_recent": sum(self.eda_history[-10:]) / min(10, len(self.eda_history)),
        }

        return stats

    def analyze_session(self, readings: List[Tuple[datetime, float]]) -> Dict:
        """Analyze a complete EDA session

        Args:
            readings: List of (timestamp, eda_value) tuples

        Returns:
            dict: Session statistics
        """
        if not readings:
            return None

        values = [v[1] for v in readings]

        # Calculate statistics
        min_val = min(values)
        max_val = max(values)
        mean_val = sum(values) / len(values)

        # Detect peaks in session
        peak_count = 0
        prev_was_peak = False

        for val in values:
            # Simple peak detection: value > mean + std_dev
            std_dev = (sum((v - mean_val) ** 2 for v in values) / len(values)) ** 0.5
            if val > mean_val + std_dev and not prev_was_peak:
                peak_count += 1
                prev_was_peak = True
            elif val <= mean_val + std_dev:
                prev_was_peak = False

        duration = (readings[-1][0] - readings[0][0]).total_seconds()

        return {
            "duration_seconds": duration,
            "reading_count": len(readings),
            "min_value": min_val,
            "max_value": max_val,
            "mean_value": mean_val,
            "range": max_val - min_val,
            "peak_count": peak_count,
            "peaks_per_minute": (peak_count / duration) * 60 if duration > 0 else 0,
        }

    def get_interpretation(self, stats: Dict) -> str:
        """Generate human-readable interpretation of EDA stats

        Args:
            stats: Statistics dict from analyze_session

        Returns:
            str: Interpretation text
        """
        if not stats:
            return "No data to analyze"

        ppm = stats["peaks_per_minute"]
        range_val = stats["range"]

        lines = [
            f"Duration: {stats['duration_seconds']:.0f}s",
            f"Readings: {stats['reading_count']}",
            f"EDA Range: {stats['min_value']:.1f} - {stats['max_value']:.1f}",
            f"Mean EDA: {stats['mean_value']:.1f}",
            f"Peak Count: {stats['peak_count']}",
            f"Peaks/min: {ppm:.1f}",
        ]

        # Interpretation based on peaks and range
        if ppm > 5:
            lines.append("→ HIGH emotional reactivity (frequent peaks)")
        elif ppm > 2:
            lines.append("→ MODERATE emotional reactivity")
        else:
            lines.append("→ LOW emotional reactivity (few peaks)")

        if range_val > 50:
            lines.append("→ LARGE dynamic range (high variation)")
        else:
            lines.append("→ SMALL dynamic range (more stable)")

        return "\n  ".join(lines)

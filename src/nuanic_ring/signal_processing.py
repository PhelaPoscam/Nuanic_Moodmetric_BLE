"""Signal conditioning for raw physiological streams."""

import math
from collections import deque

import statistics
from scipy import signal


class SignalConditioner:
    """Real-time signal conditioning pipeline (Median + Butterworth Low-Pass).

    This filter is designed to strip impulse noise (ring shifting) and smooth
    base physiological waveforms before they hit event-detecting scorers.
    """

    def __init__(
        self, sample_rate: float = 25.0, median_kernel: int = 5, cutoff_hz: float = 1.5
    ):
        self.sample_rate = sample_rate
        self.median_kernel = median_kernel
        self.cutoff_hz = cutoff_hz
        self.maxlen = median_kernel
        self.median_buffer: deque[float] = deque(maxlen=median_kernel)

        # Calculate Nyquist frequency and normalized cutoff
        nyq = 0.5 * sample_rate
        normal_cutoff = cutoff_hz / nyq

        # Create a 2nd-order Butterworth low-pass filter
        self.b, self.a = signal.butter(2, normal_cutoff, btype="low", analog=False)

        # Store the step response zero-state to prevent startup transients
        self.zi_base = signal.lfilter_zi(self.b, self.a)
        self.z = None

    def _reset_state(self) -> None:
        self.median_buffer.clear()
        self.z = None

    def process(self, value: float) -> float:
        """Process a single data point in real-time.

        Args:
            value: The raw incoming data point (e.g., Conductance in uS)

        Returns:
            The filtered data point.
        """
        if not math.isfinite(value):
            self._reset_state()
            return float("nan")

        if self.z is None:
            # Initialize with the first passed value to avoid startup drop
            self.z = self.zi_base * value
            self.median_buffer.extend([value] * self.maxlen)

        self.median_buffer.append(value)

        # 1. Median Filter (strips short impulse artifacts)
        med_val = float(statistics.median(self.median_buffer))

        # 2. Butterworth Low-Pass (smooths the waveform)
        filtered_val, self.z = signal.lfilter(self.b, self.a, [med_val], zi=self.z)

        return float(filtered_val[0])

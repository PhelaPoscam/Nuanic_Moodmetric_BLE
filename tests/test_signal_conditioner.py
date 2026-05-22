import numpy as np

from nuanic_ring.signal_processing import SignalConditioner


def test_signal_conditioner_initialization():
    conditioner = SignalConditioner(sample_rate=25.0, median_kernel=5, cutoff_hz=1.5)
    # First value processed should set the zip_state and return essentially the same value
    res = conditioner.process(10.0)
    assert abs(res - 10.0) < 0.1


def test_signal_conditioner_removes_spikes():
    conditioner = SignalConditioner(sample_rate=25.0, median_kernel=5, cutoff_hz=1.5)

    # Create a baseline of 10.0
    data = [10.0] * 50
    # Inject a massive artifact spike (Simulating ring shifting mid-session)
    data[25] = 1000.0
    data[26] = 800.0  # Two bad samples

    filtered = []
    for d in data:
        filtered.append(conditioner.process(d))

    # The artifact at index 25 and 26 shouldn't propagate past the median filter
    # and the lowpass should heavily suppress any leftover smear.
    # Without the median filter, this would cause a huge bump.
    assert max(filtered) < 11.0, f"Spike leaked through! Max was {max(filtered)}"

    # Final value should return cleanly to baseline
    assert abs(filtered[-1] - 10.0) < 0.1

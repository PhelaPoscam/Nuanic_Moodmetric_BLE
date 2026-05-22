import numpy as np
import pandas as pd

from nuanic_ring.data_analysis import (
    analyze_stress,
    calculate_correlation,
    detect_peaks,
    fit_mm_equation_from_export,
)


def test_analyze_stress():
    s = pd.Series([10.0, 20.0, 30.0, 40.0])
    res = analyze_stress(s)
    assert res is not None
    assert res["min"] == 10.0
    assert res["max"] == 40.0
    assert res["mean"] == 25.0
    assert res["range"] == 30.0


def test_detect_peaks():
    s = pd.Series([10.0] * 50 + [100.0] + [10.0] * 50)
    peaks = detect_peaks(s, threshold_std=1.5)
    assert len(peaks) == 1
    assert peaks[0]["value"] == 100.0
    assert peaks[0]["index"] == 50


def test_calculate_correlation():
    s1 = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    s2 = pd.Series([2.0, 4.0, 6.0, 8.0, 10.0])
    corr = calculate_correlation(s1, s2)
    assert abs(corr - 1.0) < 1e-6


def test_fit_mm_equation_2f():
    # We need >= 8 rows of valid srl/srrn/dne
    df = pd.DataFrame(
        {
            "srrn": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0],
            # Add slight variation so standard deviation doesn't zero out completely
            "srl": [
                100000.0,
                100001.0,
                100000.0,
                100002.0,
                100000.0,
                100001.0,
                100000.0,
                100002.0,
                100000.0,
            ],
            "dne": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0],
        }
    )

    res = fit_mm_equation_from_export(df)
    assert res is not None
    assert res["mode"] == "scrn+scl_us"
    # It linearly strongly correlates with srrn, so R2 should be perfect
    assert res["metrics"]["r2"] > 0.95

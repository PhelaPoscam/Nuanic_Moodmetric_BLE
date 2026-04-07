"""Post-session score comparison utilities for ring CSV logs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd


@dataclass
class ScoreComparison:
    """Summary statistics comparing proprietary DNE vs computed arousal."""

    path: Path
    device_mac: str
    samples_used: int
    calibrated_only: bool
    correlation: float
    mean_offset: float
    best_lag_samples: int
    best_lag_correlation: float
    d306_hz_median: float
    imu_hz_median: float


def _safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 2 or b.size < 2:
        return float("nan")
    if np.nanstd(a) <= 1e-12 or np.nanstd(b) <= 1e-12:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _best_lag_correlation(
    ours: np.ndarray,
    dne: np.ndarray,
    max_lag: int = 30,
) -> tuple[int, float]:
    """Return lag (samples) with highest Pearson correlation.

    Positive lag means our score lags behind DNE by that many samples.
    """
    if ours.size < 3 or dne.size < 3:
        return 0, float("nan")

    best_lag = 0
    best_corr = -2.0

    for lag in range(-max_lag, max_lag + 1):
        if lag > 0:
            ours_aligned = ours[lag:]
            dne_aligned = dne[: len(ours_aligned)]
        elif lag < 0:
            dne_aligned = dne[-lag:]
            ours_aligned = ours[: len(dne_aligned)]
        else:
            ours_aligned = ours
            dne_aligned = dne

        if len(ours_aligned) < 3 or len(dne_aligned) < 3:
            continue

        corr = _safe_corr(ours_aligned, dne_aligned)
        if np.isnan(corr):
            continue
        if corr > best_corr:
            best_corr = corr
            best_lag = lag

    if best_corr <= -1.5:
        return 0, float("nan")
    return best_lag, best_corr


def _analyze_single_file(path: Path) -> ScoreComparison:
    df = pd.read_csv(path)

    df["MM_Arousal_Score"] = pd.to_numeric(
        df.get("MM_Arousal_Score"),
        errors="coerce",
    )
    df["Stress_Index"] = pd.to_numeric(
        df.get("Stress_Index"),
        errors="coerce",
    )
    df["MM_Calibrated"] = pd.to_numeric(
        df.get("MM_Calibrated"),
        errors="coerce",
    ).fillna(0)
    df["D306_Observed_Hz"] = pd.to_numeric(
        df.get("D306_Observed_Hz"),
        errors="coerce",
    )
    df["IMU_Observed_Hz"] = pd.to_numeric(
        df.get("IMU_Observed_Hz"),
        errors="coerce",
    )

    valid = df[df["MM_Arousal_Score"].notna() & df["Stress_Index"].notna()].copy()
    calibrated_valid = valid[valid["MM_Calibrated"] >= 1]

    use_calibrated = len(calibrated_valid) >= 10
    working = calibrated_valid if use_calibrated else valid

    ours = working["MM_Arousal_Score"].to_numpy(dtype=float)
    dne = working["Stress_Index"].to_numpy(dtype=float)

    if len(ours) >= 2:
        correlation = _safe_corr(ours, dne)
        mean_offset = float(np.mean(ours - dne))
        lag_samples, lag_corr = _best_lag_correlation(ours, dne)
    else:
        correlation = float("nan")
        mean_offset = float("nan")
        lag_samples = 0
        lag_corr = float("nan")

    device_mac = "unknown"
    if "device_mac" in df.columns and not df["device_mac"].dropna().empty:
        device_mac = str(df["device_mac"].dropna().iloc[0])

    d306_hz_median = (
        float(df["D306_Observed_Hz"].dropna().median())
        if df["D306_Observed_Hz"].notna().any()
        else float("nan")
    )
    imu_hz_median = (
        float(df["IMU_Observed_Hz"].dropna().median())
        if df["IMU_Observed_Hz"].notna().any()
        else float("nan")
    )

    return ScoreComparison(
        path=path,
        device_mac=device_mac,
        samples_used=len(working),
        calibrated_only=use_calibrated,
        correlation=correlation,
        mean_offset=mean_offset,
        best_lag_samples=lag_samples,
        best_lag_correlation=lag_corr,
        d306_hz_median=d306_hz_median,
        imu_hz_median=imu_hz_median,
    )


def analyze_latest_ring_logs(
    log_dir: str = "data/ring_logs",
    latest_n: int = 2,
) -> List[ScoreComparison]:
    """Analyze the latest N per-ring CSV logs and return score comparisons."""
    root = Path(log_dir)
    if not root.exists():
        return []

    csv_files = sorted(
        root.glob("ring_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: max(0, int(latest_n))]

    results: List[ScoreComparison] = []
    for path in csv_files:
        try:
            results.append(_analyze_single_file(path))
        except Exception:
            continue
    return results


def format_analysis_report(results: List[ScoreComparison]) -> str:
    """Format analysis output for terminal display."""
    if not results:
        return "[POST] No log files available for analysis."

    lines = ["[POST] DNE vs Our Arousal analysis (latest ring logs):"]
    for result in results:
        lines.append(
            "- "
            f"{result.path.name} | mac={result.device_mac} | samples={result.samples_used} "
            f"| source={'calibrated' if result.calibrated_only else 'all valid'}"
        )
        lines.append(
            "  "
            f"corr={result.correlation:.4f} | mean_offset(ours-dne)={result.mean_offset:.3f} "
            f"| best_lag_samples={result.best_lag_samples} | lag_corr={result.best_lag_correlation:.4f}"
        )
        lines.append(
            "  "
            f"observed_hz_median: d306={result.d306_hz_median:.3f}, 468f={result.imu_hz_median:.3f}"
        )
    return "\n".join(lines)

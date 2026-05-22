"""Analysis helpers for Nuanic CSV logs."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def load_nuanic_csv(filepath: str) -> pd.DataFrame:
    """Load CSV file with Nuanic data using Pandas."""
    df = pd.read_csv(filepath, dtype=str)

    # Map potential legacy/modern column names
    col_map = {
        "EDA_Raw_Value": "stress_raw",
        "Stress_Index": "stress_percent",
        "payload_hex": "eda_hex",
        "full_packet_hex": "packets",
    }
    df = df.rename(columns=lambda c: col_map.get(c, c))

    # Require at least one stress measurement
    df = df.dropna(subset=["stress_raw", "stress_percent", "eda_hex"], how="all")

    # Coerce numerics safely
    df["stress_raw"] = (
        pd.to_numeric(df.get("stress_raw"), errors="coerce").fillna(0).astype(int)
    )
    df["stress_percent"] = pd.to_numeric(
        df.get("stress_percent"), errors="coerce"
    ).fillna(0.0)
    df["eda_hex"] = df.get("eda_hex", "").fillna("")
    df["packets"] = df.get("packets", "").fillna("")
    df["timestamps"] = df.get("timestamp", "")

    return df


def load_nuanic_export_csv(filepath: str) -> pd.DataFrame:
    """Load exported CSV format with columns like dne/srl/srrn/eda."""
    df = pd.read_csv(filepath)

    col_map = {"address": "device_id", "time": "timestamps", "timestamp": "timestamps"}
    df = df.rename(columns=lambda c: col_map.get(c, c))

    for col in ["dne", "srl", "srrn", "eda"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = np.nan

    df["device_id"] = df.get("device_id", "").fillna("")
    df["timestamps"] = df.get("timestamps", "").fillna("")

    return df


def fit_mm_equation_from_export(df: pd.DataFrame) -> dict[str, Any] | None:
    """Fit a simple linear MM-like equation from exported fields using vectorized OLS."""
    # Strict validation filter
    valid_df = df.dropna(subset=["dne", "srl", "srrn"]).copy()
    valid_df = valid_df[valid_df["srl"] > 0]

    if len(valid_df) < 8:
        return None

    valid_df["scl_us"] = 1_000_000.0 / valid_df["srl"]

    use_eda = "eda" in df.columns and len(valid_df.dropna(subset=["eda"])) >= 8

    if use_eda:
        mode = "scrn+scl_us+eda"
        model_df = valid_df.dropna(subset=["eda"]).copy()
        features = ["srrn", "scl_us", "eda"]
    else:
        mode = "scrn+scl_us"
        model_df = valid_df.copy()
        features = ["srrn", "scl_us"]

    y = model_df["dne"].to_numpy()
    X_raw = model_df[features].to_numpy()

    means = X_raw.mean(axis=0)
    stds = X_raw.std(axis=0, ddof=0)
    stds = np.maximum(stds, 1e-12)

    X_z = (X_raw - means) / stds
    X_mat = np.column_stack([np.ones(len(X_z)), X_z])

    # Ultra-fast OLS Solver replaces slow recursive Python block
    beta, residuals, rank, s = np.linalg.lstsq(X_mat, y, rcond=None)

    preds = X_mat @ beta
    sse = np.sum((y - preds) ** 2)
    sst = np.sum((y - y.mean()) ** 2)
    r2 = 1.0 - (sse / sst) if sst > 1e-12 else 0.0
    rmse = np.sqrt(sse / len(y))

    ret = {
        "mode": mode,
        "rows_used": len(y),
        "rows_available_2f": len(valid_df),
        "rows_available_3f": (
            len(valid_df.dropna(subset=["eda"])) if "eda" in valid_df.columns else 0
        ),
        "beta": {
            "intercept": float(beta[0]),
            "scrn_z": float(beta[1]),
            "scl_us_z": float(beta[2]),
        },
        "feature_means": {
            "scrn": float(means[0]),
            "scl_us": float(means[1]),
        },
        "feature_stds": {
            "scrn": float(stds[0]),
            "scl_us": float(stds[1]),
        },
        "metrics": {
            "r2": float(r2),
            "rmse": float(rmse),
        },
    }

    if use_eda:
        ret["beta"]["eda_z"] = float(beta[3])
        ret["feature_means"]["eda"] = float(means[2])
        ret["feature_stds"]["eda"] = float(stds[2])

    return ret


def print_export_fit_report(filepath: str) -> bool:
    """Print MM-like fit report for exported CSV data format."""
    df_raw = pd.read_csv(filepath, nrows=0)
    headers = {str(h).lower() for h in df_raw.columns}

    expected = {"dne", "srl", "srrn", "eda"}
    if not expected.issubset(headers):
        print("[INFO] CSV does not contain exported fields dne/srl/srrn/eda")
        return False

    df = load_nuanic_export_csv(filepath)
    fit = fit_mm_equation_from_export(df)

    print("\n" + "=" * 80)
    print("NUANIC EXPORTED CSV MM-LIKE FIT (VECTORED)")
    print("=" * 80)
    print(f"File: {filepath}")
    print(f"Rows total: {len(df)}")

    valid_dne = df["dne"].notna().sum()
    valid_srl = df["srl"].notna().sum()
    valid_srrn = df["srrn"].notna().sum()
    valid_eda = df["eda"].notna().sum()
    print(
        f"Valid values -> dne: {valid_dne}, srl: {valid_srl}, srrn: {valid_srrn}, eda: {valid_eda}"
    )

    if not fit:
        print("\n[WARN] Not enough valid rows to fit equation (need at least 8).")
        return True

    print(f"Fit mode: {fit['mode']}")
    print(f"Rows used for fit: {fit['rows_used']}")
    print(
        f"Rows available (2-feature): {fit['rows_available_2f']} | (3-feature): {fit['rows_available_3f']}"
    )

    b = fit["beta"]
    print("\nFitted model (standardized features):")
    if fit["mode"] == "scrn+scl_us+eda":
        print(
            f"dne ~= {b['intercept']:.4f} + {b['scrn_z']:.4f}*z(scrn) + {b['scl_us_z']:.4f}*z(scl_us) + {b['eda_z']:.4f}*z(eda)"
        )
    else:
        print(
            f"dne ~= {b['intercept']:.4f} + {b['scrn_z']:.4f}*z(scrn) + {b['scl_us_z']:.4f}*z(scl_us)"
        )

    m = fit["feature_means"]
    s = fit["feature_stds"]
    print("\nFeature standardization:")
    print(f"z(scrn)   = (scrn - {m['scrn']:.4f}) / {s['scrn']:.4f}")
    print(f"z(scl_us) = (scl_us - {m['scl_us']:.4f}) / {s['scl_us']:.4f}")
    if fit["mode"] == "scrn+scl_us+eda":
        print(f"z(eda)    = (eda - {m['eda']:.4f}) / {s['eda']:.4f}")

    print("\nFit quality:")
    print(f"R^2  = {fit['metrics']['r2']:.4f}")
    print(f"RMSE = {fit['metrics']['rmse']:.4f}")
    return True


def analyze_stress(stress_series: pd.Series) -> dict[str, float] | None:
    """Analyze stress metrics vectorized natively using Pandas."""
    if stress_series.empty:
        return None

    return {
        "min": float(stress_series.min()),
        "max": float(stress_series.max()),
        "mean": float(stress_series.mean()),
        "range": float(stress_series.max() - stress_series.min()),
        "count": len(stress_series),
    }


def analyze_eda_hex(eda_series: pd.Series) -> list[dict[str, Any]] | None:
    """Analyze EDA hex data and identify likely channels."""
    valid_hex = eda_series[eda_series.str.len() >= 4]
    if valid_hex.empty:
        return None

    channels = [[] for _ in range(4)]

    # Safe to iterate a small map logic, though parsing binary hex is non-trivial for pd masks
    for _, eda_hex in valid_hex.items():
        for i in range(4):
            offset = i * 4
            if offset + 4 <= len(eda_hex):
                h = eda_hex[offset : offset + 4]
                try:
                    channels[i].append(int(h[2:4] + h[0:2], 16))
                except ValueError:
                    pass

    active = []
    for i, c in enumerate(channels):
        if c and any(v != 0 for v in c):
            arr = np.array(c)
            active.append(
                {
                    "index": i,
                    "values": c,
                    "min": float(arr.min()),
                    "max": float(arr.max()),
                    "mean": float(arr.mean()),
                    "range": float(arr.max() - arr.min()),
                }
            )
    return active


def detect_peaks(
    series: pd.Series | list, threshold_std: float = 1.5
) -> list[dict[str, float | int]]:
    """Detect peaks vectorized instantly rather than iterating over array std deviation loops."""
    s = pd.Series(series) if isinstance(series, list) else series

    if len(s) < 3:
        return []

    mean = s.mean()
    std = s.std(ddof=0)
    threshold = mean + (threshold_std * std)

    peaks_mask = s > threshold
    peak_indices = np.where(peaks_mask)[0]

    peaks = []
    for idx in peak_indices:
        val = s.iloc[idx]
        peaks.append(
            {
                "index": int(idx),
                "value": float(val),
                "deviation": float(val - mean),
                "std_count": float((val - mean) / std) if std > 0 else 0.0,
            }
        )
    return peaks


def calculate_correlation(
    s1: pd.Series | list[float], s2: pd.Series | list[float]
) -> float:
    """Vectorized Pearson correlation leveraging pure C."""
    if isinstance(s1, list):
        s1 = pd.Series(s1)
    if isinstance(s2, list):
        s2 = pd.Series(s2)
    return float(s1.corr(s2))


def print_report(filepath: str) -> None:
    """Generate analysis report."""
    print("\n" + "=" * 80)
    print("NUANIC RING DATA ANALYSIS REPORT (VECTORED)")
    print("=" * 80)
    print(f"File: {filepath}")
    print("=" * 80 + "\n")

    try:
        df = load_nuanic_csv(filepath)
    except Exception as exc:
        print(f"[ERROR] Failed to load file: {exc}")
        return

    if df.empty:
        print("[ERROR] No data found in file")
        return

    print("SESSION INFORMATION")
    print("-" * 80)
    print(f"Total readings: {len(df)}")

    timestamps = df["timestamps"][df["timestamps"] != ""].values
    if len(timestamps) > 0:
        print(f"Start: {timestamps[0]}")
        print(f"End: {timestamps[-1]}")
        try:
            start = datetime.fromisoformat(str(timestamps[0]))
            end = datetime.fromisoformat(str(timestamps[-1]))
            duration = (end - start).total_seconds()
            print(f"Duration: {duration:.1f} seconds ({duration / 60:.1f} minutes)")
        except Exception:
            pass
    print()

    print("STRESS ANALYSIS")
    print("-" * 80)
    stress_stats = analyze_stress(df["stress_percent"])
    if stress_stats:
        print(f"Minimum: {stress_stats['min']:.1f}%")
        print(f"Maximum: {stress_stats['max']:.1f}%")
        print(f"Mean: {stress_stats['mean']:.1f}%")
        print(f"Range: {stress_stats['range']:.1f}%")

        peaks = detect_peaks(df["stress_percent"], threshold_std=1.5)
        print(f"Peak count (> 1.5σ): {len(peaks)}")
        if peaks:
            avg_peak = sum(p["value"] for p in peaks) / len(peaks)
            print(f"Average peak value: {avg_peak:.1f}%")
            print(
                f"Peaks per minute: {(len(peaks) / (stress_stats['count'] / 60)):.1f}"
            )
    print()

    print("EDA DATA ANALYSIS")
    print("-" * 80)
    eda_channels = analyze_eda_hex(df["eda_hex"])

    if eda_channels:
        print(f"Active channels detected: {len(eda_channels)}\n")
        for channel in eda_channels:
            print(f"Channel {channel['index']}:")
            print(f"  Range: {channel['min']} - {channel['max']}")
            print(f"  Mean: {channel['mean']:.1f}")
            print(f"  Variation: {channel['range']}")

            channel_peaks = detect_peaks(channel["values"], threshold_std=1.5)
            print(f"  Peaks: {len(channel_peaks)}")

            if len(channel["values"]) == len(df):
                correlation = calculate_correlation(
                    channel["values"], df["stress_percent"]
                )
                print(f"  Correlation with stress: {correlation:.3f}")
            print()
    else:
        print("Could not parse EDA channels from hex data")

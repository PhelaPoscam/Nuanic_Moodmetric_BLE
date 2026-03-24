"""Analysis helpers for Nuanic CSV logs."""

from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path
from typing import Any


def load_nuanic_csv(filepath: str) -> dict[str, list[Any]]:
    """Load CSV file with Nuanic data."""
    data = {
        "timestamps": [],
        "stress_raw": [],
        "stress_percent": [],
        "eda_hex": [],
        "packets": [],
    }

    with open(filepath, "r", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            data["timestamps"].append(row["timestamp"])
            data["stress_raw"].append(int(row["stress_raw"]))
            data["stress_percent"].append(float(row["stress_percent"]))
            data["eda_hex"].append(row["eda_hex"])
            data["packets"].append(row["full_packet_hex"])

    return data


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"\\n", "null", "none", "nan"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def load_nuanic_export_csv(filepath: str) -> dict[str, list[Any]]:
    """Load exported CSV format with columns like dne/srl/srrn/eda."""
    data = {
        "device_id": [],
        "timestamps": [],
        "dne": [],
        "srl": [],
        "srrn": [],
        "eda": [],
    }

    with open(filepath, "r", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            data["device_id"].append(row.get("address") or row.get("device_id") or "")
            data["timestamps"].append(row.get("time") or row.get("timestamp") or "")
            data["dne"].append(_to_float_or_none(row.get("dne")))
            data["srl"].append(_to_float_or_none(row.get("srl")))
            data["srrn"].append(_to_float_or_none(row.get("srrn")))
            data["eda"].append(_to_float_or_none(row.get("eda")))

    return data


def _gaussian_solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve Ax=b using Gaussian elimination with partial pivoting."""
    n = len(a)
    aug = [row[:] + [b[i]] for i, row in enumerate(a)]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]

        div = aug[col][col]
        aug[col] = [v / div for v in aug[col]]

        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if abs(factor) < 1e-12:
                continue
            aug[r] = [aug[r][c] - factor * aug[col][c] for c in range(n + 1)]

    return [aug[i][n] for i in range(n)]


def fit_mm_equation_from_export(data: dict[str, list[Any]]) -> dict[str, Any] | None:
    """Fit a simple linear MM-like equation from exported fields.

    Model:
      dne ~= b0 + b1*scrn + b2*scl_us + b3*eda

    Where:
      scrn   <- srrn
      scl_us <- 1e6 / srl  (assuming srl is resistance in ohms)
      eda    <- eda
    """
    rows3: list[tuple[float, float, float, float]] = []
    rows2: list[tuple[float, float, float]] = []
    for y, srl, srrn, eda in zip(
        data["dne"], data["srl"], data["srrn"], data["eda"], strict=False
    ):
        if y is None or srrn is None or srl is None or srl <= 0:
            continue
        scl_us = 1_000_000.0 / srl
        rows2.append((float(y), float(srrn), float(scl_us)))
        if eda is not None:
            rows3.append((float(y), float(srrn), float(scl_us), float(eda)))

    # Prefer full model with EDA when enough overlap exists; otherwise fallback.
    if len(rows3) >= 8:
        mode = "scrn+scl_us+eda"
        y_values = [r[0] for r in rows3]
        x_means = [sum(r[i] for r in rows3) / len(rows3) for i in (1, 2, 3)]
        x_stds = []
        for i in (1, 2, 3):
            m = x_means[i - 1]
            var = sum((r[i] - m) ** 2 for r in rows3) / len(rows3)
            x_stds.append(max(1e-12, math.sqrt(var)))

        mtx = [[0.0] * 4 for _ in range(4)]
        vec = [0.0] * 4
        for y, scrn, scl_us, eda in rows3:
            x = [
                1.0,
                (scrn - x_means[0]) / x_stds[0],
                (scl_us - x_means[1]) / x_stds[1],
                (eda - x_means[2]) / x_stds[2],
            ]
            for i in range(4):
                vec[i] += x[i] * y
                for j in range(4):
                    mtx[i][j] += x[i] * x[j]

        beta = _gaussian_solve(mtx, vec)
        if beta is None:
            return None

        preds = []
        for _y, scrn, scl_us, eda in rows3:
            x = [
                1.0,
                (scrn - x_means[0]) / x_stds[0],
                (scl_us - x_means[1]) / x_stds[1],
                (eda - x_means[2]) / x_stds[2],
            ]
            preds.append(sum(beta[i] * x[i] for i in range(4)))

        sse = sum((y_values[i] - preds[i]) ** 2 for i in range(len(rows3)))
        sst = sum((y - (sum(y_values) / len(y_values))) ** 2 for y in y_values)

        return {
            "mode": mode,
            "rows_used": len(rows3),
            "rows_available_2f": len(rows2),
            "rows_available_3f": len(rows3),
            "beta": {
                "intercept": beta[0],
                "scrn_z": beta[1],
                "scl_us_z": beta[2],
                "eda_z": beta[3],
            },
            "feature_means": {
                "scrn": x_means[0],
                "scl_us": x_means[1],
                "eda": x_means[2],
            },
            "feature_stds": {
                "scrn": x_stds[0],
                "scl_us": x_stds[1],
                "eda": x_stds[2],
            },
            "metrics": {
                "r2": 1.0 - (sse / sst) if sst > 1e-12 else 0.0,
                "rmse": math.sqrt(sse / len(rows3)),
            },
        }

    if len(rows2) < 8:
        return None

    mode = "scrn+scl_us"
    y_values = [r[0] for r in rows2]
    x_means = [sum(r[i] for r in rows2) / len(rows2) for i in (1, 2)]
    x_stds = []
    for i in (1, 2):
        m = x_means[i - 1]
        var = sum((r[i] - m) ** 2 for r in rows2) / len(rows2)
        x_stds.append(max(1e-12, math.sqrt(var)))

    # Normal equations for 3 params: intercept + 2 standardized features.
    mtx = [[0.0] * 3 for _ in range(3)]
    vec = [0.0] * 3
    for y, scrn, scl_us in rows2:
        x = [1.0, (scrn - x_means[0]) / x_stds[0], (scl_us - x_means[1]) / x_stds[1]]
        for i in range(3):
            vec[i] += x[i] * y
            for j in range(3):
                mtx[i][j] += x[i] * x[j]

    beta = _gaussian_solve(mtx, vec)
    if beta is None:
        return None

    preds = []
    for _y, scrn, scl_us in rows2:
        x = [1.0, (scrn - x_means[0]) / x_stds[0], (scl_us - x_means[1]) / x_stds[1]]
        preds.append(sum(beta[i] * x[i] for i in range(3)))

    y_mean = sum(y_values) / len(y_values)
    sse = sum((y_values[i] - preds[i]) ** 2 for i in range(len(rows2)))
    sst = sum((y - y_mean) ** 2 for y in y_values)
    r2 = 1.0 - (sse / sst) if sst > 1e-12 else 0.0
    rmse = math.sqrt(sse / len(rows2))

    return {
        "mode": mode,
        "rows_used": len(rows2),
        "rows_available_2f": len(rows2),
        "rows_available_3f": len(rows3),
        "beta": {
            "intercept": beta[0],
            "scrn_z": beta[1],
            "scl_us_z": beta[2],
        },
        "feature_means": {
            "scrn": x_means[0],
            "scl_us": x_means[1],
        },
        "feature_stds": {
            "scrn": x_stds[0],
            "scl_us": x_stds[1],
        },
        "metrics": {
            "r2": r2,
            "rmse": rmse,
        },
    }


def print_export_fit_report(filepath: str) -> bool:
    """Print MM-like fit report for exported CSV data format."""
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = {h.lower() for h in (reader.fieldnames or [])}

    expected = {"dne", "srl", "srrn", "eda"}
    if not expected.issubset(headers):
        print("[INFO] CSV does not contain exported fields dne/srl/srrn/eda")
        return False

    data = load_nuanic_export_csv(filepath)
    fit = fit_mm_equation_from_export(data)

    print("\n" + "=" * 80)
    print("NUANIC EXPORTED CSV MM-LIKE FIT")
    print("=" * 80)
    print(f"File: {filepath}")
    print(f"Rows total: {len(data['timestamps'])}")

    valid_dne = sum(1 for v in data["dne"] if v is not None)
    valid_srl = sum(1 for v in data["srl"] if v is not None)
    valid_srrn = sum(1 for v in data["srrn"] if v is not None)
    valid_eda = sum(1 for v in data["eda"] if v is not None)
    print(
        "Valid values -> "
        f"dne: {valid_dne}, srl: {valid_srl}, srrn: {valid_srrn}, eda: {valid_eda}"
    )

    if not fit:
        print("\n[WARN] Not enough valid rows to fit equation (need at least 8).")
        print("       Capture/export a session with non-null dne/srl/srrn/eda.")
        return True

    print(f"Fit mode: {fit['mode']}")
    print(f"Rows used for fit: {fit['rows_used']}")
    print(
        f"Rows available (2-feature): {fit['rows_available_2f']} | "
        f"(3-feature): {fit['rows_available_3f']}"
    )
    print("\nFitted model (standardized features):")
    b = fit["beta"]
    if fit["mode"] == "scrn+scl_us+eda":
        print(
            "dne ~= "
            f"{b['intercept']:.4f} + {b['scrn_z']:.4f}*z(scrn) + "
            f"{b['scl_us_z']:.4f}*z(scl_us) + {b['eda_z']:.4f}*z(eda)"
        )
    else:
        print(
            "dne ~= "
            f"{b['intercept']:.4f} + {b['scrn_z']:.4f}*z(scrn) + "
            f"{b['scl_us_z']:.4f}*z(scl_us)"
        )

    print("\nFeature standardization:")
    means = fit["feature_means"]
    stds = fit["feature_stds"]
    print(f"z(scrn)   = (scrn - {means['scrn']:.4f}) / {stds['scrn']:.4f}")
    print(f"z(scl_us) = (scl_us - {means['scl_us']:.4f}) / {stds['scl_us']:.4f}")
    if fit["mode"] == "scrn+scl_us+eda":
        print(f"z(eda)    = (eda - {means['eda']:.4f}) / {stds['eda']:.4f}")

    m = fit["metrics"]
    print("\nFit quality:")
    print(f"R^2  = {m['r2']:.4f}")
    print(f"RMSE = {m['rmse']:.4f}")

    return True


def analyze_stress(data: dict[str, list[Any]]) -> dict[str, float] | None:
    """Analyze stress metrics."""
    stress = data["stress_percent"]

    if not stress:
        return None

    return {
        "min": min(stress),
        "max": max(stress),
        "mean": sum(stress) / len(stress),
        "range": max(stress) - min(stress),
        "count": len(stress),
    }


def analyze_eda_hex(eda_hex_list: list[str]) -> list[dict[str, Any]] | None:
    """Analyze EDA hex data and identify likely channels."""
    if not eda_hex_list:
        return None

    channels = [[] for _ in range(4)]

    for eda_hex in eda_hex_list:
        try:
            for i in range(4):
                offset = i * 4
                if offset + 4 <= len(eda_hex):
                    hex_val = eda_hex[offset : offset + 4]
                    int_val = int(hex_val[2:4] + hex_val[0:2], 16)
                    channels[i].append(int_val)
        except Exception:
            pass

    active_channels = []
    for i, channel in enumerate(channels):
        if channel and any(v != 0 for v in channel):
            active_channels.append(
                {
                    "index": i,
                    "values": channel,
                    "min": min(channel),
                    "max": max(channel),
                    "mean": sum(channel) / len(channel),
                    "range": max(channel) - min(channel),
                }
            )

    return active_channels


def detect_peaks(
    values: list[float] | list[int], threshold_std: float = 1.5
) -> list[dict[str, float | int]]:
    """Detect peaks in stress/EDA data."""
    if len(values) < 3:
        return []

    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std_dev = variance**0.5

    threshold = mean + (threshold_std * std_dev)

    peaks = []
    for i, value in enumerate(values):
        if value > threshold:
            peaks.append(
                {
                    "index": i,
                    "value": value,
                    "deviation": value - mean,
                    "std_count": ((value - mean) / std_dev if std_dev > 0 else 0),
                }
            )

    return peaks


def calculate_correlation(
    x: list[float] | list[int], y: list[float] | list[int]
) -> float:
    """Calculate Pearson correlation coefficient."""
    if len(x) != len(y) or len(x) < 2:
        return 0.0

    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)

    numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(len(x)))

    sum_sq_x = sum((xi - mean_x) ** 2 for xi in x)
    sum_sq_y = sum((yi - mean_y) ** 2 for yi in y)

    denominator = (sum_sq_x * sum_sq_y) ** 0.5

    if denominator == 0:
        return 0.0

    return numerator / denominator


def print_report(filepath: str) -> None:
    """Generate analysis report."""
    print("\n" + "=" * 80)
    print("NUANIC RING DATA ANALYSIS REPORT")
    print("=" * 80)
    print(f"File: {filepath}")
    print("=" * 80 + "\n")

    try:
        data = load_nuanic_csv(filepath)
    except Exception as exc:
        print(f"[ERROR] Failed to load file: {exc}")
        return

    if not data["timestamps"]:
        print("[ERROR] No data found in file")
        return

    print("SESSION INFORMATION")
    print("-" * 80)
    print(f"Total readings: {len(data['timestamps'])}")
    print(f"Start: {data['timestamps'][0]}")
    print(f"End: {data['timestamps'][-1]}")

    try:
        start = datetime.fromisoformat(data["timestamps"][0])
        end = datetime.fromisoformat(data["timestamps"][-1])
        duration = (end - start).total_seconds()
        print(f"Duration: {duration:.1f} seconds ({duration / 60:.1f} minutes)")
    except Exception:
        duration = None

    print()

    print("STRESS ANALYSIS")
    print("-" * 80)
    stress_stats = analyze_stress(data)
    if stress_stats:
        print(f"Minimum: {stress_stats['min']:.1f}%")
        print(f"Maximum: {stress_stats['max']:.1f}%")
        print(f"Mean: {stress_stats['mean']:.1f}%")
        print(f"Range: {stress_stats['range']:.1f}%")

        peaks = detect_peaks(data["stress_percent"], threshold_std=1.5)
        print(f"Peak count (> 1.5σ): {len(peaks)}")
        if peaks:
            avg_peak = sum(p["value"] for p in peaks) / len(peaks)
            print(f"Average peak value: {avg_peak:.1f}%")
            print(
                "Peaks per minute: "
                f"{(len(peaks) / (stress_stats['count'] / 60)):.1f}"
            )

    print()

    print("EDA DATA ANALYSIS")
    print("-" * 80)
    eda_channels = analyze_eda_hex(data["eda_hex"])

    if eda_channels:
        print(f"Active channels detected: {len(eda_channels)}\n")
        for channel in eda_channels:
            print(f"Channel {channel['index']}:")
            print(f"  Range: {channel['min']} - {channel['max']}")
            print(f"  Mean: {channel['mean']:.1f}")
            print(f"  Variation: {channel['range']}")

            channel_peaks = detect_peaks(channel["values"], threshold_std=1.5)
            print(f"  Peaks: {len(channel_peaks)}")

            if len(channel["values"]) == len(data["stress_percent"]):
                correlation = calculate_correlation(
                    channel["values"], data["stress_percent"]
                )
                print(f"  Correlation with stress: {correlation:.3f}")
            print()
    else:
        print("Could not parse EDA channels from hex data")
        print("First few EDA hex values:")
        for i, eda in enumerate(data["eda_hex"][:3]):
            print(f"  Reading {i}: {eda[:32]}...")

    print()

    print("RECOMMENDATIONS")
    print("-" * 80)
    if stress_stats and stress_stats["range"] < 20:
        print("• Low stress variation - session was stable")
    elif stress_stats and stress_stats["range"] > 50:
        print("• High stress variation - detected multiple stressors")

    if eda_channels and len(eda_channels) > 0:
        print(f"• Found {len(eda_channels)} EDA channels " "- further analysis needed")
        print("• Study correlations between channels")
        print("• Compare EDA peaks with stress transitions")

    print("\nNext steps:")
    print("1. Plot stress over time to visualize changes")
    print("2. Analyze EDA channels to understand sensor data")
    print("3. Cross-correlate stress with EDA for validation")
    print("4. Compare with simultaneous HR data if available")


def main(args: list[str] | None = None) -> int:
    """CLI entrypoint for analysis."""
    if args is None:
        import sys

        args = sys.argv[1:]

    if len(args) < 1:
        print("Usage: python analyze_nuanic_data.py <csv_file>")
        print()
        print("Examples:")
        print(
            "  python analyze_nuanic_data.py " "data/nuanic_logs/nuanic_2024-01-15.csv"
        )
        print("  python analyze_nuanic_data.py data/nuanic_logs/nuanic_*")
        return 1

    filepath = args[0]

    if not Path(filepath).exists():
        print(f"[ERROR] File not found: {filepath}")
        return 1

    print_report(filepath)
    return 0

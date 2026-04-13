#!/usr/bin/env python3
"""Post-session DNE vs computed arousal analysis for latest ring logs."""

import argparse
import sys

try:
    from nuanic_ring.post_analysis import (
        analyze_latest_ring_logs,
        format_analysis_report,
    )
except ModuleNotFoundError:
    print(
        "[ERROR] Could not import 'nuanic_ring'. "
        "Install the project first: pip install -e .[dev]"
    )
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze latest ring CSV logs (DNE vs computed arousal)."
    )
    parser.add_argument(
        "--log-dir",
        default="data/ring_logs",
        help="Directory containing ring_*.csv logs.",
    )
    parser.add_argument(
        "--latest",
        type=int,
        default=2,
        help="How many latest ring CSV files to analyze (default: 2).",
    )
    args = parser.parse_args()

    results = analyze_latest_ring_logs(log_dir=args.log_dir, latest_n=args.latest)
    print(format_analysis_report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

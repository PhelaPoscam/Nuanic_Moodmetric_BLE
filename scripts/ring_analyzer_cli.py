#!/usr/bin/env python3
"""Ring data analyzer CLI - Analyze logged CSV data."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.data_analysis import print_export_fit_report, print_report


def main():
    parser = argparse.ArgumentParser(
        description="Analyze ring CSV log files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s data/ring_logs/nuanic_2026-03-05_15-45-19.csv
    %(prog)s data/ring_logs/nuanic_stress_2026-03-05_15-45-19.csv
    %(prog)s data/ring_logs/*.csv
        """,
    )

    parser.add_argument(
        "filepath",
        help="Path to CSV log file to analyze",
    )
    parser.add_argument(
        "--fit-mm",
        action="store_true",
        help=(
            "Attempt MM-like equation fit when CSV has exported fields "
            "(dne,srl,srrn,eda)"
        ),
    )

    args = parser.parse_args()

    filepath = Path(args.filepath)

    if not filepath.exists():
        print(f"[ERROR] File not found: {filepath}")
        return 1

    try:
        if args.fit_mm:
            fit_handled = print_export_fit_report(str(filepath))
            if fit_handled:
                return 0
        print_report(str(filepath))
        return 0
    except Exception as e:
        print(f"\n[ERROR] Analysis failed: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

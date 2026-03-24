#!/usr/bin/env python3
"""Ring logger CLI - Log stress and EDA data to CSV."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.connector import NuanicConnector
from nuanic_ring.logger import NuanicDataLogger


async def main():
    parser = argparse.ArgumentParser(
        description="Log ring data (stress + EDA) to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Scan and select ring, log indefinitely
  %(prog)s --duration 300               # Log for 5 minutes
  %(prog)s --list-rings                 # List all available rings
  %(prog)s --ring-addr 58:A3:D0:95:DF:2D --duration 60
        """,
    )

    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Duration in seconds (default: unlimited, Ctrl+C to stop)",
    )
    parser.add_argument(
        "--log-dir",
        default="data/ring_logs",
        help="Directory to save CSV logs (default: data/ring_logs)",
    )
    parser.add_argument(
        "--ring-addr",
        default=None,
        help="BLE address of ring (e.g., 58:A3:D0:95:DF:2D). If not provided, will prompt.",
    )
    parser.add_argument(
        "--list-rings",
        action="store_true",
        help="List available rings and exit",
    )

    args = parser.parse_args()

    # Handle --list-rings
    if args.list_rings:
        try:
            connector = NuanicConnector()
            rings = await connector.list_available_rings()

            if not rings:
                print("\n[FAIL] No compatible rings found\n")
                return

            print(f"\n✓ Found {len(rings)} ring device(s):\n")
            for i, ring in enumerate(rings, 1):
                print(f"  {i}. {ring['name']:20} | {ring['address']}")
            print()
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n[STOP] Scan cancelled\n")
        return

    # Create and run logger
    try:
        logger = NuanicDataLogger(log_dir=args.log_dir)
        # If ring address is provided, pin to it. Otherwise connector will prompt at connect time.
        if args.ring_addr:
            logger.connector.target_address = args.ring_addr

        print(
            f"\n[LOGGER] Starting with ring: {args.ring_addr if args.ring_addr else 'interactive selection'}"
        )
        print(
            f"[LOGGER] Duration: {args.duration if args.duration else 'unlimited'} seconds"
        )
        print("[LOGGER] Logs saved to:", args.log_dir)

        await logger.start_logging(duration_seconds=args.duration)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[STOP] Logger stopped")
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

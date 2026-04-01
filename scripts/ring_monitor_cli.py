#!/usr/bin/env python3
"""Ring monitor CLI.

Behavior by detected profile:
- nuanic: full decoded monitor (IMU + stress/state)
- Moodmetric: generic notify capture (UUID, payload len, raw hex)
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.connector import NuanicConnector
from nuanic_ring.monitor import NuanicMonitor
from nuanic_ring.waveform_viewer import run_waveform_viewer


async def main():
    parser = argparse.ArgumentParser(
        description="Real-time ring monitor (IMU + stress + EDA)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Scan and select from available rings
  %(prog)s --duration 60                # Monitor for 60 seconds
  %(prog)s --list-rings                 # List all available rings
  %(prog)s --ring-addr 58:A3:D0:95:DF:2D --duration 30
    %(prog)s --waveform                   # Open live waveform viewer
    %(prog)s --waveform --window-seconds 20 --refresh-ms 100
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
    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument(
        "--log",
        dest="enable_logging",
        action="store_true",
        help="Enable CSV logging (default)",
    )
    log_group.add_argument(
        "--no-log",
        dest="enable_logging",
        action="store_false",
        help="Disable CSV logging",
    )
    parser.set_defaults(enable_logging=True)
    parser.add_argument(
        "--imu-refresh",
        type=int,
        default=5,
        help="Refresh display every N IMU packets (default: 5)",
    )
    parser.add_argument(
        "--calibration-seconds",
        type=int,
        default=60,
        help="MM-like index calibration period in seconds (default: 60)",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Don't clear terminal on refresh",
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
    parser.add_argument(
        "--waveform",
        action="store_true",
        help="Enable live waveform viewer instead of text monitor",
    )
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=10,
        help="Waveform mode: approximate visible window in seconds (default: 10)",
    )
    parser.add_argument(
        "--refresh-ms",
        type=int,
        default=120,
        help="Waveform mode: plot refresh interval in milliseconds (default: 120)",
    )
    parser.add_argument(
        "--smooth",
        type=int,
        default=1,
        metavar="WINDOW",
        help="Waveform mode: smoothing window size (1=none, 5-10=light, 15-30=heavy, default: 1)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover all ring services and characteristics, then exit",
    )

    args = parser.parse_args()

    # Handle --discover
    if args.discover:
        try:
            connector = NuanicConnector(target_address=args.ring_addr)
            if not await connector.connect():
                print("[FAIL] Could not connect to ring")
                return
            print("\n" + "=" * 70)
            print("RING GATT DISCOVERY")
            print("=" * 70)
            await connector.discover_services()
            await connector.disconnect()
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n[STOP] Discovery cancelled")
        except Exception as e:
            print(f"[FAIL] Discovery error: {e}")
        return

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

    # Run waveform mode when requested.
    if args.waveform:
        try:
            print(
                f"\n[WAVEFORM] Starting with ring: {args.ring_addr if args.ring_addr else 'interactive selection'}"
            )
            smooth_desc = "none" if args.smooth <= 1 else f"{args.smooth}-point"
            print(
                f"[WAVEFORM] Window: {args.window_seconds}s | Refresh: {args.refresh_ms}ms | Smooth: {smooth_desc}"
            )
            await run_waveform_viewer(
                ring_addr=args.ring_addr,
                window_seconds=args.window_seconds,
                refresh_ms=args.refresh_ms,
                smooth_window=args.smooth,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n[STOP] Waveform viewer stopped")
        return

    # Create and run text monitor.
    try:
        monitor = NuanicMonitor(
            log_dir=args.log_dir,
            imu_refresh_packets=args.imu_refresh,
            clear_console=not args.no_clear,
            enable_logging=args.enable_logging,
            calibration_seconds=args.calibration_seconds,
        )
        # If ring address is provided, pin to it. Otherwise connector will prompt at connect time.
        if args.ring_addr:
            monitor.connector.target_address = args.ring_addr

        print(
            f"\n[MONITOR] Starting with ring: {args.ring_addr if args.ring_addr else 'interactive selection'}"
        )
        print("[MONITOR] UUID map:")
        print(
            "  STATE_UUID=3c180fcc-bfec-4b7c-8e52-1a37f123e449 | "
            "STORAGE_UUID=7c3b82e7-22b7-4cb6-8458-ba325edf6ede"
        )
        print(
            "  LIVE_EDA_UUID=42dcb71b-1817-43bd-8ea3-7272780a1c9f | "
            "LIVE_DNA_UUID=d306262b-c8c9-4c4b-9050-3a41dea706e5"
        )
        print(
            "  SET_TIME_UUID=dc9c31a7-fbd3-467a-8777-10900c423d3b | "
            "SAMPLE_RATE_UUID=516b0fb6-d861-4619-9dd0-0105e8b85128"
        )
        print(
            "  STORAGE_FORMAT_UUID=3cce21a7-e602-4e02-8c52-1e0366c1c846 | "
            "BATTERY_UUID=00002a19-0000-1000-8000-00805f9b34fb"
        )
        print(
            f"[MONITOR] Duration: {args.duration if args.duration else 'unlimited'} seconds"
        )
        if args.enable_logging:
            print("[MONITOR] Logging: enabled")
            print("[MONITOR] Logs saved to:", args.log_dir)
        else:
            print("[MONITOR] Logging: disabled")

        await monitor.run(duration_seconds=args.duration)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[STOP] Monitor stopped")
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

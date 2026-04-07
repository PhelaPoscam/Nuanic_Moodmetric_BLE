#!/usr/bin/env python3
"""Multi-ring monitor CLI with Rich live dashboard."""

# pyright: reportMissingImports=false, reportMissingModuleSource=false
# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportUnknownLambdaType=false

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

from rich.console import Console  # type: ignore[import-not-found]
from rich.live import Live  # type: ignore[import-not-found]
from rich.table import Table  # type: ignore[import-not-found]

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _parse_ring_addresses(
    ring_addr: str,
    ring_addrs: str,
) -> List[str]:
    addresses: List[str] = []
    if ring_addr:
        addresses.append(ring_addr.strip())
    if ring_addrs:
        addresses.extend(
            [a.strip() for a in ring_addrs.split(",") if a.strip()]
        )

    dedup: List[str] = []
    seen: set[str] = set()
    for addr in addresses:
        key = addr.upper()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(key)
    return dedup


def _build_dashboard_table(
    rows: List[Dict[str, Any]],
    elapsed_seconds: float,
):
    table = Table(
        title=(
            "Nuanic Multi-Ring Dashboard"
            f"  |  Elapsed: {elapsed_seconds:.1f}s"
        )
    )
    table.add_column("Device MAC", style="cyan")
    table.add_column("Connection Status", style="magenta")
    table.add_column("Battery", style="green")
    table.add_column("Raw EDA", justify="right")
    table.add_column("Filtered uS", justify="right")
    table.add_column("Our Arousal (1-100)", justify="right")
    table.add_column("Ring DNE (0-100)", justify="right")
    table.add_column("Obs Hz D306/468F", justify="right")
    table.add_column("Rate Ctrl")
    table.add_column("IMU (X,Y,Z)")

    if not rows:
        table.add_row(
            "-",
            "no devices",
            "-",
            "-",
            "-",
            "-",
            "-",
            "-",
            "-",
            "-",
        )
        return table

    for row in rows:
        table.add_row(
            row["device_mac"],
            row["connection_status"],
            row["battery"],
            row["raw_eda"],
            row["filtered_us"],
            row["arousal_score"],
            row["dne_score"],
            row["observed_hz"],
            row["rate_control"],
            row["imu_xyz"],
        )

    return table


async def main():
    from nuanic_ring.connector import NuanicConnector
    from nuanic_ring.monitor import NuanicMonitor
    from nuanic_ring.post_analysis import (
        analyze_latest_ring_logs,
        format_analysis_report,
    )
    from nuanic_ring.waveform_viewer import run_waveform_viewer

    parser = argparse.ArgumentParser(
        description="Real-time ring monitor (single or multi-ring)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --monitor-all
  %(prog)s --ring-addrs 58:A3:D0:95:DF:2D,AA:BB:CC:DD:EE:FF --stagger-delay 1.5
  %(prog)s --ring-addr 58:A3:D0:95:DF:2D --duration 60
  %(prog)s --monitor-all --no-auto-reconnect
  %(prog)s --waveform --ring-addr 58:A3:D0:95:DF:2D
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
        help="Reserved for compatibility (default: 5)",
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
        help="Reserved for compatibility in rich mode",
    )

    # Compatibility single-ring option.
    parser.add_argument(
        "--ring-addr",
        default=None,
        help="Single BLE address (legacy-compatible)",
    )

    # New multi-ring options.
    parser.add_argument(
        "--ring-addrs",
        default=None,
        help="Comma-separated BLE addresses for explicit multi-ring mode",
    )
    parser.add_argument(
        "--monitor-all",
        action="store_true",
        help="Discover and monitor all visible Nuanic/Moodmetric rings",
    )
    parser.add_argument(
        "--max-devices",
        type=int,
        default=None,
        help="Optional cap on concurrently monitored devices",
    )
    parser.add_argument(
        "--stagger-delay",
        type=float,
        default=1.25,
        help="Delay between connection attempts in seconds (default: 1.25)",
    )

    reconnect_group = parser.add_mutually_exclusive_group()
    reconnect_group.add_argument(
        "--auto-reconnect",
        dest="auto_reconnect",
        action="store_true",
        help="Enable auto-reconnect mode (default)",
    )
    reconnect_group.add_argument(
        "--no-auto-reconnect",
        dest="auto_reconnect",
        action="store_false",
        help="Disable auto-reconnect and mark rings offline on disconnect",
    )
    parser.set_defaults(auto_reconnect=True)

    parser.add_argument(
        "--ui-refresh-ms",
        type=int,
        default=200,
        help="Rich dashboard refresh interval in ms (default: 200)",
    )
    parser.add_argument(
        "--target-hz",
        type=float,
        default=10.0,
        help=(
            "Target stream rate used for diagnostics/equalization policy "
            "(default: 10)"
        ),
    )
    parser.add_argument(
        "--rate-control",
        choices=["yes", "no"],
        default="yes",
        help="Attempt ring-side sample-rate write on connect (default: yes)",
    )
    parser.add_argument(
        "--equalize-mode",
        choices=["off", "log-only", "enforce"],
        default="log-only",
        help="Host-side equalization policy (default: log-only)",
    )
    parser.add_argument(
        "--post-analysis",
        choices=["yes", "no"],
        default="no",
        help=(
            "After monitoring, analyze latest log files and print DNE vs "
            "computed-arousal metrics (default: no)."
        ),
    )
    parser.add_argument(
        "--posanalysys",
        dest="post_analysis",
        choices=["yes", "no"],
        help=argparse.SUPPRESS,
    )

    parser.add_argument(
        "--list-rings",
        action="store_true",
        help="List available rings and exit",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help=(
            "Discover all ring services/characteristics "
            "for one target and exit"
        ),
    )

    parser.add_argument(
        "--waveform",
        action="store_true",
        help="Enable waveform viewer instead of table dashboard",
    )
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=10,
        help="Waveform mode: approximate visible window in seconds",
    )
    parser.add_argument(
        "--refresh-ms",
        type=int,
        default=120,
        help="Waveform mode: plot refresh interval in milliseconds",
    )
    parser.add_argument(
        "--smooth",
        type=int,
        default=1,
        metavar="WINDOW",
        help="Waveform mode: smoothing window size",
    )

    args = parser.parse_args()
    console = Console()

    if args.discover:
        connector = NuanicConnector(target_address=args.ring_addr)
        if not await connector.connect():
            console.print("[red][FAIL] Could not connect to ring[/red]")
            return
        try:
            await connector.discover_services()
        finally:
            await connector.disconnect()
        return

    if args.list_rings:
        connector = NuanicConnector()
        rings = await connector.list_available_rings_with_paired()
        if not rings:
            console.print("[yellow][WARN] No compatible rings found[/yellow]")
            return

        console.print(f"\nFound {len(rings)} ring(s):")
        for i, ring in enumerate(rings, 1):
            source = ring.get("source", "scan")
            console.print(
                f"  {i}. {ring['name']:20} | {ring['address']} | {source}"
            )
        return

    if args.waveform:
        await run_waveform_viewer(
            ring_addr=args.ring_addr,
            window_seconds=args.window_seconds,
            refresh_ms=args.refresh_ms,
            smooth_window=args.smooth,
        )
        return

    monitor = NuanicMonitor(
        log_dir=args.log_dir,
        imu_refresh_packets=args.imu_refresh,
        clear_console=not args.no_clear,
        enable_logging=args.enable_logging,
        calibration_seconds=args.calibration_seconds,
        target_hz=args.target_hz,
        equalize_mode=args.equalize_mode,
        attempt_ring_rate_control=(args.rate_control == "yes"),
    )

    explicit_addresses = _parse_ring_addresses(args.ring_addr, args.ring_addrs)
    monitor_all = args.monitor_all

    if not explicit_addresses and not monitor_all:
        console.print(
            "Starting in legacy single-ring mode "
            "(interactive ring selection)."
        )
    elif explicit_addresses:
        console.print(
            f"Starting explicit mode for {len(explicit_addresses)} ring(s)."
        )
    else:
        console.print("Starting monitor-all discovery mode.")

    started = await monitor.start_multi(
        ring_addresses=explicit_addresses or None,
        monitor_all=monitor_all,
        max_devices=args.max_devices,
        stagger_delay=max(0.0, args.stagger_delay),
        auto_reconnect=args.auto_reconnect,
    )

    if not started:
        console.print("[red][FAIL] Could not start monitoring any ring[/red]")
        return

    refresh_interval = max(0.05, args.ui_refresh_ms / 1000.0)
    started_at = asyncio.get_event_loop().time()

    try:
        with Live(
            console=console,
            refresh_per_second=max(1, int(1 / refresh_interval)),
        ) as live:
            while True:
                elapsed = asyncio.get_event_loop().time() - started_at
                rows = monitor.dashboard_rows()
                live.update(_build_dashboard_table(rows, elapsed))

                if args.duration is not None and elapsed >= args.duration:
                    break

                await asyncio.sleep(refresh_interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await monitor.stop_multi()

    if args.post_analysis == "yes" and args.enable_logging:
        results = analyze_latest_ring_logs(log_dir=args.log_dir, latest_n=2)
        console.print(format_analysis_report(results))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

#!/usr/bin/env python3
"""Multi-ring monitor CLI with Rich live dashboard."""

# pyright: reportMissingImports=false, reportMissingModuleSource=false
# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportUnknownLambdaType=false

import argparse
import asyncio
import sys
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from rich.console import Console  # type: ignore[import-not-found]
from rich.console import Group  # type: ignore[import-not-found]
from rich.live import Live  # type: ignore[import-not-found]
from rich.table import Table  # type: ignore[import-not-found]
from rich.text import Text  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from nuanic_ring.monitor import NuanicMonitor


def _stdout_encoding_is_utf8() -> bool:
    encoding = (sys.stdout.encoding or "").lower()
    return encoding == "utf-8"


# --- Global Encoding Fix for Windows ---
if sys.platform == "win32":
    try:
        if not _stdout_encoding_is_utf8():
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
# ----------------------------------------


def _parse_ring_addresses(
    ring_addr: str,
    ring_addrs: str,
) -> List[str]:
    addresses: List[str] = []
    if ring_addr:
        addresses.append(ring_addr.strip())
    if ring_addrs:
        addresses.extend([a.strip() for a in ring_addrs.split(",") if a.strip()])

    dedup: List[str] = []
    seen: set[str] = set()
    for addr in addresses:
        key = addr.upper()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(key)
    return dedup


def _parse_marker_hotkey_spec(spec: str) -> Optional[Tuple[str, str]]:
    """Parse a hotkey spec of the form KEY=LABEL."""
    text = spec.strip()
    if not text or "=" not in text:
        return None

    key_text, label_text = text.split("=", 1)
    key = key_text.strip().upper()
    label = label_text.strip()
    if not key or not label:
        return None
    return key, label


def _default_marker_hotkeys() -> Dict[str, str]:
    return {
        "SPACE": "marker",
        "S": "stimulus_on",
        "B": "baseline_start",
        "R": "rest_start",
    }


def _build_marker_hotkeys(specs: List[str]) -> Dict[str, str]:
    hotkeys = _default_marker_hotkeys()
    for spec in specs:
        parsed = _parse_marker_hotkey_spec(spec)
        if parsed is None:
            continue
        key, label = parsed
        hotkeys[key] = label
    return hotkeys


def _format_marker_legend(hotkeys: Dict[str, str]) -> str:
    parts = []
    for key in sorted(hotkeys):
        parts.append(f"{key}={hotkeys[key]}")
    return " | ".join(parts)


def _build_dashboard_table(
    rows: List[Dict[str, Any]],
    elapsed_seconds: float,
    box_style: Any = None,
    marker_legend: str = "",
):
    table = Table(
        title=("Nuanic Multi-Ring Dashboard" f"  |  Elapsed: {elapsed_seconds:.1f}s"),
        box=box_style,
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
    if marker_legend:
        table.caption = f"Markers: {marker_legend}"

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


def _build_dashboard_renderable(
    rows: List[Dict[str, Any]],
    elapsed_seconds: float,
    box_style: Any = None,
    marker_legend: str = "",
):
    table = _build_dashboard_table(
        rows,
        elapsed_seconds,
        box_style=box_style,
        marker_legend=marker_legend,
    )
    if not marker_legend:
        return table

    legend = Text(f"Marker keys: {marker_legend}", style="dim")
    return Group(legend, table)


class _NonBlockingLineReader:
    """Non-blocking stdin reader for runtime marker input and hotkeys."""

    def __init__(self, hotkeys: Dict[str, str]) -> None:
        self._buffer = ""
        self._win_msvcrt = None
        self._last_space_ts = 0.0
        self._hotkeys = {key.upper(): value for key, value in hotkeys.items()}
        if sys.platform == "win32":
            import msvcrt

            self._win_msvcrt = msvcrt

    def poll_markers(self) -> List[str]:
        markers: List[str] = []
        if self._win_msvcrt is not None:
            while self._win_msvcrt.kbhit():
                ch = self._win_msvcrt.getwch()
                if ch == " ":
                    now = time.monotonic()
                    if (now - self._last_space_ts) >= 0.18:
                        marker = self._hotkeys.get("SPACE")
                        if marker:
                            markers.append(marker)
                        self._last_space_ts = now
                    continue
                if len(ch) == 1:
                    marker = self._hotkeys.get(ch.upper())
                    if marker:
                        markers.append(marker)
                        continue
                if ch in ("\r", "\n"):
                    line = self._buffer
                    self._buffer = ""
                    label = _parse_marker_label(line)
                    if label:
                        markers.append(label)
                    continue
                if ch in ("\b", "\x7f"):
                    self._buffer = self._buffer[:-1]
                    continue
                if ch in ("\x00", "\xe0"):
                    if self._win_msvcrt.kbhit():
                        self._win_msvcrt.getwch()
                    continue
                if ch == "\x03":
                    raise KeyboardInterrupt
                self._buffer += ch
            return markers

        import select

        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if not readable:
            return markers

        line = sys.stdin.readline()
        if not line:
            return markers
        label = _parse_marker_label(line.rstrip("\r\n"))
        if label:
            markers.append(label)
        return markers


def _parse_marker_label(raw_line: str) -> str | None:
    """Parse accepted marker commands from user input."""
    line = raw_line.strip()
    if not line:
        return None

    lower = line.lower()
    if lower.startswith("/m "):
        label = line[3:].strip()
        return label or None
    if lower.startswith("marker "):
        label = line[7:].strip()
        return label or None
    if line.startswith("/"):
        return None
    return line


def _poll_marker_input(
    reader: _NonBlockingLineReader,
    monitor: "NuanicMonitor",
) -> None:
    labels = reader.poll_markers()
    for label in labels:
        source = "keypress" if label else "stdin"
        inserted = monitor.add_marker(label=label, source=source)
        if inserted > 0:
            print(f"[MARKER] '{label}' inserted into {inserted} device log(s)")


def build_parser() -> argparse.ArgumentParser:
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
            "(Note: empirical multi-ring ceiling is ~16Hz, default: 10)"
        ),
    )
    parser.add_argument(
        "--force-hz",
        action="store_true",
        help="Bypass the 16Hz hardware-safety cap for multi-ring sessions (DANGER)",
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
        "--use-warmup",
        action="store_true",
        help="Enable 'Theory of Two' disconnect/reconnect warmup sequence",
    )
    parser.add_argument(
        "--warmup-delay",
        type=float,
        default=3.0,
        help="Delay in seconds after firmware warmup before full connect (default: 3.0)",
    )
    parser.add_argument(
        "--reset-bt",
        action="store_true",
        help="Enable aggressive Windows BT radio reset if initial connection fails",
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
        help="Discover all ring services/characteristics for one target and exit",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=6.0,
        help="Timeout per scan attempt in seconds (default: 6.0)",
    )
    parser.add_argument(
        "--scan-attempts",
        type=int,
        default=3,
        help="Number of scan attempts to perform (default: 3)",
    )

    parser.add_argument(
        "--waveform",
        action="store_true",
        help="Enable waveform viewer instead of table dashboard",
    )
    parser.add_argument(
        "--markers",
        action="store_true",
        help="Enable runtime marker input (hotkeys and '/m LABEL' + Enter)",
    )
    parser.add_argument(
        "--marker-hotkey",
        action="append",
        default=[],
        metavar="KEY=LABEL",
        help=(
            "Add or override a single-key marker hotkey. Repeatable, "
            "for example: --marker-hotkey S=stimulus"
        ),
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

    return parser


async def main():
    try:
        from nuanic_ring.connector import NuanicConnector
        from nuanic_ring.monitor import NuanicMonitor
        from nuanic_ring.post_analysis import (
            analyze_latest_ring_logs,
            format_analysis_report,
        )
        from nuanic_ring.waveform_viewer import run_waveform_viewer
    except ModuleNotFoundError:
        print(
            "[ERROR] Could not import 'nuanic_ring'. "
            "Install the project first: pip install -e .[dev]"
        )
        return

    parser = build_parser()

    # On Windows, force ASCII box-characters if the environment is not UTF-8 capable,
    # or if we want maximum robustness.
    box_style = None
    ascii_box = None
    if sys.platform == "win32" and not _stdout_encoding_is_utf8():
        from rich import box

        box_style = box.ASCII
        ascii_box = box.ASCII

    args = parser.parse_args()
    console = Console(force_terminal=True, soft_wrap=True)

    target_addresses = []
    if args.ring_addrs:
        target_addresses = [a.strip() for a in args.ring_addrs.split(",") if a.strip()]

    # Multiple rings are unstable above 16 Hz
    is_multi = args.monitor_all or len(target_addresses) > 1
    if is_multi and args.target_hz and args.target_hz > 16:
        if args.force_hz:
            console.print(
                f"\n[bold red]DANGER: HIGH FREQUENCY SESSION FORCED "
                f"({args.target_hz} Hz)[/bold red]\n"
                f"Multi-ring sessions above 16 Hz are unstable and may "
                f"freeze the ring firmware.\n"
            )
        else:
            console.print(
                f"\n[bold yellow]STABILITY WARNING:[/bold yellow] "
                f"Multi-ring sessions are unstable above 16 Hz. "
                f"Capping [bold cyan]{args.target_hz} Hz[/bold cyan] -> "
                f"[bold green]16 Hz[/bold green].\n"
            )
            args.target_hz = 16

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
        rings = await connector.list_available_rings_with_paired(
            scan_timeout=args.scan_timeout, attempts=args.scan_attempts
        )
        if not rings:
            console.print("[yellow][WARN] No compatible rings found[/yellow]")
            return

        console.print(
            f"\nFound {len(rings)} ring(s) [Attempts: {args.scan_attempts}, Window: {args.scan_timeout}s]:"
        )
        for i, ring in enumerate(rings, 1):
            source = ring.get("source", "scan")
            console.print(f"  {i}. {ring['name']:20} | {ring['address']} | {source}")
        return

    if args.waveform:
        await run_waveform_viewer(
            ring_addr=args.ring_addr,
            window_seconds=args.window_seconds,
            refresh_ms=args.refresh_ms,
            smooth_window=args.smooth,
            target_hz=args.target_hz,
            attempt_rate_control=(args.rate_control == "yes"),
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
        force_hz=args.force_hz,
        use_warmup=args.use_warmup,
        warmup_delay=args.warmup_delay,
        allow_reset_bt=args.reset_bt,
    )

    explicit_addresses = _parse_ring_addresses(args.ring_addr, args.ring_addrs)
    marker_hotkeys = _build_marker_hotkeys(args.marker_hotkey)
    monitor_all = args.monitor_all

    if not explicit_addresses and not monitor_all:
        console.print(
            "Starting in legacy single-ring mode " "(interactive ring selection)."
        )
    elif explicit_addresses:
        console.print(f"Starting explicit mode for {len(explicit_addresses)} ring(s).")
    else:
        console.print("Starting monitor-all discovery mode.")

    started = await monitor.start_multi(
        ring_addresses=explicit_addresses or None,
        monitor_all=monitor_all,
        max_devices=args.max_devices,
        stagger_delay=max(0.0, args.stagger_delay),
        auto_reconnect=args.auto_reconnect,
        scan_timeout=args.scan_timeout,
        scan_attempts=args.scan_attempts,
    )

    if not started:
        console.print("[red][FAIL] Could not start monitoring any ring[/red]")
        return

    refresh_interval = max(0.05, args.ui_refresh_ms / 1000.0)
    started_at = asyncio.get_event_loop().time()
    marker_reader = _NonBlockingLineReader(marker_hotkeys) if args.markers else None

    if args.markers:
        marker_legend = _format_marker_legend(marker_hotkeys)
        console.print(
            f"[cyan]Markers enabled:[/cyan] {marker_legend}. "
            "Press [bold]SPACE[/bold] for marker or type [bold]/m LABEL[/bold] "
            "+ Enter for a custom label."
        )
        console.print(
            "[dim]Tip: use SPACE/S/B/R during the session to mark events in real time.[/dim]"
        )
    else:
        marker_legend = ""

    try:
        with Live(
            console=console,
            refresh_per_second=max(1, int(1 / refresh_interval)),
        ) as live:
            while True:
                elapsed = asyncio.get_event_loop().time() - started_at
                rows = monitor.dashboard_rows()
                try:
                    renderable = _build_dashboard_renderable(
                        rows,
                        elapsed,
                        box_style=box_style,
                        marker_legend=marker_legend,
                    )
                    live.update(renderable)
                except UnicodeEncodeError:
                    # Fallback for stray unicode characters in rows
                    safe_rows = []
                    for r in rows:
                        sr = {
                            k: (
                                "".join(c for c in str(v) if ord(c) < 128)
                                if isinstance(v, str)
                                else v
                            )
                            for k, v in r.items()
                        }
                        safe_rows.append(sr)
                    live.update(
                        _build_dashboard_renderable(
                            safe_rows,
                            elapsed,
                            box_style=(ascii_box if sys.platform == "win32" else None),
                            marker_legend=marker_legend,
                        )
                    )

                if args.duration is not None and elapsed >= args.duration:
                    break

                if marker_reader is not None:
                    _poll_marker_input(marker_reader, monitor)

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

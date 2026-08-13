"""Consolidated command-line interfaces for Nuanic Ring BLE tools."""

import argparse
import asyncio
import sys
import time
from typing import Any, Dict, List

from nuanic_ring import (
    MODE_LIVE,
    MODE_RAW_EDA,
    MODE_RESEARCH,
    MODE_STANDBY,
    NuanicConnector,
)
from nuanic_ring.discover_services import run_diagnostics
from nuanic_ring.monitor import NuanicMonitor

_MODE_MAP = {
    "live": MODE_LIVE,
    "research": MODE_RESEARCH,
    "raw_eda": MODE_RAW_EDA,
    "standby": MODE_STANDBY,
}


def _configure_windows_console() -> None:
    """Force UTF-8 output on Windows consoles so rich box-drawing renders cleanly.

    Without this, the legacy console codepage (e.g. cp437/cp1252) mangles
    non-ASCII glyphs (mojibake). Errors are replaced rather than crashing so
    the dashboard never dies on a stray character.
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def _check_dependency(module_name: str, extra: str = "cli") -> bool:
    try:
        __import__(module_name)
        return True
    except ImportError:
        print(
            f"Error: The '{module_name}' module is required for this command/option but is not installed.\n"
            f"Please install it using: pip install nuanic-ring[{extra}]",
            file=sys.stderr,
        )
        return False


def _parse_ring_addresses(ring_addr: str, ring_addrs: str) -> List[str]:
    addresses = []
    if ring_addr:
        addresses.append(ring_addr.strip())
    if ring_addrs:
        addresses.extend([a.strip() for a in ring_addrs.split(",") if a.strip()])

    dedup = []
    seen = set()
    for addr in addresses:
        key = addr.upper()
        if key not in seen:
            seen.add(key)
            dedup.append(key)
    return dedup


def _build_marker_hotkeys(specs: List[str]) -> Dict[str, str]:
    hotkeys = {
        "SPACE": "marker",
        "S": "stimulus_on",
        "B": "baseline_start",
        "R": "rest_start",
    }
    for spec in specs:
        text = spec.strip()
        if not text or "=" not in text:
            continue
        key_text, label_text = text.split("=", 1)
        key = key_text.strip().upper()
        label = label_text.strip()
        if key and label:
            hotkeys[key] = label
    return hotkeys


def _format_marker_legend(hotkeys: Dict[str, str]) -> str:
    return " | ".join(f"{k}={hotkeys[k]}" for k in sorted(hotkeys))


def _build_dashboard_table(
    rows: List[Dict[str, Any]],
    elapsed_seconds: float,
    box_style: Any = None,
    marker_legend: str = "",
) -> Any:
    from rich.table import Table

    table = Table(
        title=f"Nuanic Multi-Ring Dashboard  |  Elapsed: {elapsed_seconds:.1f}s",
        box=box_style,
    )
    table.add_column("MAC", style="cyan", no_wrap=True)
    table.add_column("Status", style="magenta")
    table.add_column("Batt", style="green")
    table.add_column("EDA (Ohm)", justify="right")
    table.add_column("EDA (uS)", justify="right")
    table.add_column("Ring DNE", justify="right")
    table.add_column("Obs Hz", justify="right")
    table.add_column("Rate Ctrl")
    table.add_column("IMU (X,Y,Z)")
    if marker_legend:
        table.caption = f"Markers: {marker_legend}"

    if not rows:
        table.add_row(*(["-"] * 9))
        return table

    for r in rows:
        table.add_row(
            r["device_mac"],
            r["connection_status"],
            r["battery"],
            r["raw_eda"],
            r["filtered_us"],
            r["dne_score"],
            r["observed_hz"],
            r["rate_control"],
            r["imu_xyz"],
        )
    return table


def _build_dashboard_renderable(
    rows: List[Dict[str, Any]],
    elapsed_seconds: float,
    box_style: Any = None,
    marker_legend: str = "",
) -> Any:
    from rich.console import Group
    from rich.text import Text

    table = _build_dashboard_table(
        rows,
        elapsed_seconds,
        box_style=box_style,
        marker_legend=marker_legend,
    )
    if not marker_legend:
        return table

    return Group(Text(f"Marker keys: {marker_legend}", style="dim"), table)


def _parse_marker_label(raw_line: str) -> str | None:
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
    return line


class _NonBlockingLineReader:
    def __init__(self, hotkeys: Dict[str, str]) -> None:
        self._buffer = ""
        self._win_msvcrt = None
        self._last_space_ts = 0.0
        self._hotkeys = {k.upper(): v for k, v in hotkeys.items()}
        if sys.platform == "win32":
            import msvcrt

            self._win_msvcrt = msvcrt

    def poll_markers(self) -> List[str]:
        markers = []
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
        if readable:
            line = sys.stdin.readline()
            if line:
                label = _parse_marker_label(line.rstrip("\r\n"))
                if label:
                    markers.append(label)
        return markers


def _poll_marker_input(reader: _NonBlockingLineReader, monitor: NuanicMonitor) -> None:
    labels = reader.poll_markers()
    for label in labels:
        source = "keypress" if label in reader._hotkeys.values() else "stdin"
        inserted = monitor.add_marker(label=label, source=source)
        if inserted > 0:
            print(f"[MARKER] '{label}' inserted into {inserted} device log(s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Real-time ring monitor (single or multi-ring)"
    )
    parser.add_argument(
        "--duration", type=int, default=None, help="Duration in seconds"
    )
    parser.add_argument(
        "--log-dir", default="data/ring_logs", help="Directory to save CSV logs"
    )
    parser.add_argument(
        "--participant-id", type=str, default=None, help="Participant ID"
    )

    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument(
        "--log", dest="enable_logging", action="store_true", default=True
    )
    log_group.add_argument("--no-log", dest="enable_logging", action="store_false")

    parser.add_argument(
        "--csv-layout", choices=["combined", "split", "both"], default="combined"
    )
    parser.add_argument(
        "--nuanic-export", action="store_true", help="Output CSV in exact Nuanic format"
    )
    parser.add_argument("--imu-refresh", type=int, default=5)
    parser.add_argument("--no-clear", action="store_true")
    parser.add_argument("--ring-addr", default=None)
    parser.add_argument("--ring-addrs", default=None)
    parser.add_argument("--monitor-all", action="store_true")
    parser.add_argument("--max-devices", type=int, default=None)
    parser.add_argument("--stagger-delay", type=float, default=1.25)

    reconnect_group = parser.add_mutually_exclusive_group()
    reconnect_group.add_argument(
        "--auto-reconnect", dest="auto_reconnect", action="store_true", default=True
    )
    reconnect_group.add_argument(
        "--no-auto-reconnect", dest="auto_reconnect", action="store_false"
    )

    parser.add_argument("--ui-refresh-ms", type=int, default=200)
    parser.add_argument("--target-hz", type=float, default=10.0)
    parser.add_argument("--force-hz", action="store_true")
    parser.add_argument("--rate-control", choices=["yes", "no"], default="yes")
    parser.add_argument(
        "--equalize-mode", choices=["off", "log-only", "enforce"], default="log-only"
    )
    parser.add_argument("--use-warmup", action="store_true")
    parser.add_argument("--warmup-delay", type=float, default=3.0)
    parser.add_argument("--reset-bt", action="store_true")
    parser.add_argument(
        "--filter",
        action="store_true",
        default=False,
        help="Apply signal conditioner (median + Butterworth lowpass) to EDA stream. Off by default.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        default=False,
        help="(Deprecated) Alias for default no-filter behavior. Ignored.",
    )
    parser.add_argument("--list-rings", action="store_true")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument(
        "--subscribe-streams",
        action="store_true",
        help="[with --discover] Subscribe to ring notify streams and print live data",
    )
    parser.add_argument(
        "--listen-seconds",
        type=int,
        default=None,
        help="[with --discover --subscribe-streams] Duration in seconds (default: until Ctrl+C)",
    )
    parser.add_argument("--scan-timeout", type=float, default=6.0)
    parser.add_argument("--scan-attempts", type=int, default=3)
    parser.add_argument(
        "--mode",
        choices=["live", "research", "raw_eda", "standby"],
        default="raw_eda",
        help="Ring operational mode (default: raw_eda for unfiltered skin conductance)",
    )
    parser.add_argument("--waveform", action="store_true")
    parser.add_argument("--markers", action="store_true")
    parser.add_argument("--marker-hotkey", action="append", default=[])
    parser.add_argument("--window-seconds", type=int, default=10)
    parser.add_argument("--refresh-ms", type=int, default=120)
    parser.add_argument("--smooth", type=int, default=1)
    parser.add_argument(
        "--check-flash",
        action="store_true",
        help="Check offline flash memory usage and status",
    )
    parser.add_argument(
        "--sync-time",
        action="store_true",
        help="Manually synchronize ring clock with system time",
    )
    parser.add_argument(
        "--reset-algo",
        action="store_true",
        help="Send command 'ra' to reset onboard DNE algorithm",
    )
    parser.add_argument(
        "--shipping-mode",
        action="store_true",
        help="Send command 'sm' to put device in shipping mode",
    )
    parser.add_argument(
        "--download-storage",
        action="store_true",
        help="Download all offline recorded session records from flash",
    )
    return parser


async def _execute_maintenance(
    args: argparse.Namespace, connector: NuanicConnector, console: Any
) -> None:
    """Execute one-off maintenance commands against a connected ring."""
    if args.sync_time:
        res = await connector.sync_time()
        console.print(
            "[green][SUCCESS] Time synchronized:[/green] True"
            if res
            else "[red][FAIL] Time sync failed[/red]"
        )
    if args.reset_algo:
        res = await connector.send_command("ra")
        console.print(
            "[green][SUCCESS] DNE Algorithm reset:[/green] True"
            if res
            else "[red][FAIL] Reset algo failed[/red]"
        )
    if args.shipping_mode:
        res = await connector.send_command("sm")
        console.print(
            "[green][SUCCESS] Sent shipping mode command:[/green] True"
            if res
            else "[red][FAIL] Shipping mode failed[/red]"
        )
    if args.check_flash:
        usage = await connector.read_storage_usage()
        fmt = await connector.read_storage_format()
        if usage:
            console.print("\n[bold cyan]OFFLINE FLASH MEMORY STATUS:[/bold cyan]")
            console.print(f"  Total Size     : {usage['size_bytes']} bytes")
            console.print(
                f"  Used Space     : {usage['used_bytes']} bytes ({usage['percent_used']:.1f}%)"
            )
            console.print(f"  Available      : {usage['available_bytes']} bytes")
            fmt_str = (
                "Standby/None"
                if fmt == 0
                else ("Raw EDA (14B)" if fmt == 1 else "Nuanic Algorithm DNE (22B)")
            )
            console.print(f"  Storage Format : {fmt} ({fmt_str})\n")
        else:
            console.print("[red][FAIL] Could not read storage usage[/red]")
    if args.download_storage:
        console.print(
            "[cyan]Downloading offline session records from flash memory...[/cyan]"
        )
        records = await connector.download_storage()
        console.print(
            f"[green]Downloaded {len(records)} record(s) from flash storage.[/green]"
        )
        if records:
            console.print(f"Sample first record: {records[0]}")
            console.print(f"Sample last record : {records[-1]}")


async def _run_maintenance_commands(args: argparse.Namespace, console: Any) -> int:
    """Handle one-off device maintenance commands (flash check, time sync, etc.)."""
    connector = NuanicConnector(
        target_address=args.ring_addr, auto_sync_time=not args.sync_time
    )
    if not await connector.connect():
        console.print("[red][FAIL] Could not connect to ring[/red]")
        return 1
    try:
        await _execute_maintenance(args, connector, console)
    finally:
        await connector.disconnect()
    return 0


def ring_monitor() -> int:
    """Entry point for nuanic-ring-monitor command."""
    _configure_windows_console()
    if not _check_dependency("rich"):
        return 1
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.waveform:
            if not _check_dependency("matplotlib") or not _check_dependency("numpy"):
                return 1
            from nuanic_ring.waveform_viewer import run_waveform_viewer_sync

            return run_waveform_viewer_sync(
                ring_addr=args.ring_addr,
                window_seconds=args.window_seconds,
                refresh_ms=args.refresh_ms,
                smooth_window=args.smooth,
                target_hz=args.target_hz,
                attempt_rate_control=(args.rate_control == "yes"),
                apply_filter=args.filter,
                enable_logging=args.enable_logging,
                log_dir=args.log_dir,
                participant_id=args.participant_id,
                csv_layout=args.csv_layout,
                initial_mode=_MODE_MAP[args.mode],
            )
        return asyncio.run(_run_monitor_cli(args))
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user. Exiting...")
        return 0


def _cap_multi_ring_hz(console: Any, args: argparse.Namespace) -> None:
    """Warn or cap target_hz for multi-ring sessions (hardware unstable above ~16 Hz)."""
    is_multi = (
        args.monitor_all
        or len(_parse_ring_addresses(args.ring_addr, args.ring_addrs)) > 1
    )
    if not is_multi or not args.target_hz or args.target_hz <= 16:
        return
    if args.force_hz:
        console.print(
            f"\n[bold red]DANGER: HIGH FREQUENCY SESSION FORCED ({args.target_hz} Hz)[/bold red]\n"
        )
    else:
        console.print(
            f"\n[bold yellow]STABILITY WARNING:[/bold yellow] Capping {args.target_hz} Hz -> 16 Hz.\n"
        )
        args.target_hz = 16


async def _run_monitor_cli(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.live import Live

    console = Console(force_terminal=True, emoji=False)
    box_style = None
    if sys.platform == "win32" and (sys.stdout.encoding or "").lower() != "utf-8":
        from rich import box

        box_style = box.ASCII

    target_addresses = _parse_ring_addresses(args.ring_addr, args.ring_addrs)
    _cap_multi_ring_hz(console, args)

    if args.discover:
        connector = NuanicConnector(target_address=args.ring_addr)
        if not await connector.connect():
            console.print("[red][FAIL] Could not connect to ring[/red]")
            return 1
        try:
            await run_diagnostics(
                connector.client,
                subscribe_streams=args.subscribe_streams,
                listen_seconds=args.listen_seconds,
            )
        finally:
            await connector.disconnect()
        return 0

    if (
        args.check_flash
        or args.sync_time
        or args.reset_algo
        or args.shipping_mode
        or args.download_storage
    ):
        return await _run_maintenance_commands(args, console)

    if args.nuanic_export:
        args.csv_layout = "nuanic"

    if args.list_rings:
        connector = NuanicConnector()
        rings = await connector.list_available_rings_with_paired(
            scan_timeout=args.scan_timeout, attempts=args.scan_attempts
        )
        if not rings:
            console.print("[yellow][WARN] No compatible rings found[/yellow]")
            return 0
        console.print(f"\nFound {len(rings)} ring(s):")
        for i, ring in enumerate(rings, 1):
            console.print(
                f"  {i}. {ring['name']:20} | {ring['address']} | {ring.get('source', 'scan')}"
            )
        return 0

    # Waveform check is intercepted synchronously in ring_monitor() to avoid Matplotlib GUI thread issues

    monitor = NuanicMonitor(
        log_dir=args.log_dir,
        imu_refresh_packets=args.imu_refresh,
        clear_console=not args.no_clear,
        enable_logging=args.enable_logging,
        csv_layout=args.csv_layout,
        target_hz=args.target_hz,
        equalize_mode=args.equalize_mode,
        attempt_ring_rate_control=(args.rate_control == "yes"),
        force_hz=args.force_hz,
        use_warmup=args.use_warmup,
        warmup_delay=args.warmup_delay,
        allow_reset_bt=args.reset_bt,
        participant_id=args.participant_id,
        apply_filter=args.filter,
        initial_mode=_MODE_MAP[args.mode],
    )

    started = await monitor.start_multi(
        ring_addresses=target_addresses or None,
        monitor_all=args.monitor_all,
        max_devices=args.max_devices,
        stagger_delay=max(0.0, args.stagger_delay),
        auto_reconnect=args.auto_reconnect,
        scan_timeout=args.scan_timeout,
        scan_attempts=args.scan_attempts,
    )

    if not started:
        console.print("[red][FAIL] Could not start monitoring any ring[/red]")
        return 1

    refresh_interval = max(0.05, args.ui_refresh_ms / 1000.0)
    started_at = asyncio.get_event_loop().time()
    marker_hotkeys = _build_marker_hotkeys(args.marker_hotkey)
    marker_reader = _NonBlockingLineReader(marker_hotkeys) if args.markers else None

    if args.markers:
        console.print(
            f"[cyan]Markers enabled:[/cyan] {_format_marker_legend(marker_hotkeys)}"
        )

    try:
        with Live(
            console=console, refresh_per_second=int(1 / refresh_interval)
        ) as live:
            while True:
                elapsed = asyncio.get_event_loop().time() - started_at
                rows = monitor.dashboard_rows()
                try:
                    renderable = _build_dashboard_renderable(
                        rows,
                        elapsed,
                        box_style,
                        _format_marker_legend(marker_hotkeys) if args.markers else "",
                    )
                    live.update(renderable)
                except UnicodeEncodeError:
                    safe_rows = [
                        {
                            k: (
                                "".join(c for c in str(v) if ord(c) < 128)
                                if isinstance(v, str)
                                else v
                            )
                            for k, v in r.items()
                        }
                        for r in rows
                    ]
                    live.update(
                        _build_dashboard_renderable(
                            safe_rows,
                            elapsed,
                            box_style,
                            (
                                _format_marker_legend(marker_hotkeys)
                                if args.markers
                                else ""
                            ),
                        )
                    )

                if args.duration is not None and elapsed >= args.duration:
                    break
                if marker_reader:
                    _poll_marker_input(marker_reader, monitor)
                await asyncio.sleep(refresh_interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await monitor.stop_multi()

    return 0


if __name__ == "__main__":
    raise SystemExit(ring_monitor())

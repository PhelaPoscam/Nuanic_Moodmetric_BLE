"""Consolidated command-line interfaces for Nuanic Ring BLE tools."""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from nuanic_ring.connector import NuanicConnector
from nuanic_ring.monitor import NuanicMonitor
from nuanic_ring.post_analysis import (
    analyze_latest_ring_logs,
    format_analysis_report,
)
from nuanic_ring.waveform_viewer import run_waveform_viewer


def _stdout_encoding_is_utf8() -> bool:
    return (sys.stdout.encoding or "").lower() == "utf-8"


if sys.platform == "win32":
    try:
        if not _stdout_encoding_is_utf8():
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


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


def _parse_marker_hotkey_spec(spec: str) -> Optional[Tuple[str, str]]:
    text = spec.strip()
    if not text or "=" not in text:
        return None
    key_text, label_text = text.split("=", 1)
    key = key_text.strip().upper()
    label = label_text.strip()
    return (key, label) if key and label else None


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
        if parsed:
            hotkeys[parsed[0]] = parsed[1]
    return hotkeys


def _format_marker_legend(hotkeys: Dict[str, str]) -> str:
    return " | ".join(f"{k}={hotkeys[k]}" for k in sorted(hotkeys))


def _build_dashboard_table(
    rows: List[Dict[str, Any]],
    elapsed_seconds: float,
    box_style: Any = None,
    marker_legend: str = "",
) -> Table:
    table = Table(
        title=f"Nuanic Multi-Ring Dashboard  |  Elapsed: {elapsed_seconds:.1f}s",
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
        table.add_row(*(["-"] * 10))
        return table

    for r in rows:
        table.add_row(
            r["device_mac"],
            r["connection_status"],
            r["battery"],
            r["raw_eda"],
            r["filtered_us"],
            r["arousal_score"],
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
    if line.startswith("/"):
        return None
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
    parser.add_argument("--imu-refresh", type=int, default=5)
    parser.add_argument("--calibration-seconds", type=int, default=60)
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
        "--raw", action="store_true", help="Bypass signal conditioner, stream raw EDA"
    )
    parser.add_argument("--post-analysis", choices=["yes", "no"], default="no")
    parser.add_argument("--list-rings", action="store_true")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--scan-timeout", type=float, default=6.0)
    parser.add_argument("--scan-attempts", type=int, default=3)
    parser.add_argument("--waveform", action="store_true")
    parser.add_argument("--markers", action="store_true")
    parser.add_argument("--marker-hotkey", action="append", default=[])
    parser.add_argument("--window-seconds", type=int, default=10)
    parser.add_argument("--refresh-ms", type=int, default=120)
    parser.add_argument("--smooth", type=int, default=1)
    return parser


def ring_monitor() -> int:
    """Entry point for nuanic-ring-monitor command."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run_monitor_cli(args))
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user. Exiting...")
        return 0


async def _run_monitor_cli(args: argparse.Namespace) -> int:
    console = Console(force_terminal=True)
    box_style = None
    if sys.platform == "win32" and not _stdout_encoding_is_utf8():
        from rich import box

        box_style = box.ASCII

    target_addresses = _parse_ring_addresses(args.ring_addr, args.ring_addrs)
    is_multi = args.monitor_all or len(target_addresses) > 1

    if is_multi and args.target_hz and args.target_hz > 16:
        if args.force_hz:
            console.print(
                f"\n[bold red]DANGER: HIGH FREQUENCY SESSION FORCED ({args.target_hz} Hz)[/bold red]\n"
            )
        else:
            console.print(
                f"\n[bold yellow]STABILITY WARNING:[/bold yellow] Capping {args.target_hz} Hz -> 16 Hz.\n"
            )
            args.target_hz = 16

    if args.discover:
        connector = NuanicConnector(target_address=args.ring_addr)
        if not await connector.connect():
            console.print("[red][FAIL] Could not connect to ring[/red]")
            return 1
        try:
            await connector.discover_services()
        finally:
            await connector.disconnect()
        return 0

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

    if args.waveform:
        return await run_waveform_viewer(
            ring_addr=args.ring_addr,
            window_seconds=args.window_seconds,
            refresh_ms=args.refresh_ms,
            smooth_window=args.smooth,
            target_hz=args.target_hz,
            attempt_rate_control=(args.rate_control == "yes"),
            raw_signal=args.raw,
        )

    monitor = NuanicMonitor(
        log_dir=args.log_dir,
        imu_refresh_packets=args.imu_refresh,
        clear_console=not args.no_clear,
        enable_logging=args.enable_logging,
        csv_layout=args.csv_layout,
        calibration_seconds=args.calibration_seconds,
        target_hz=args.target_hz,
        equalize_mode=args.equalize_mode,
        attempt_ring_rate_control=(args.rate_control == "yes"),
        force_hz=args.force_hz,
        use_warmup=args.use_warmup,
        warmup_delay=args.warmup_delay,
        allow_reset_bt=args.reset_bt,
        participant_id=args.participant_id,
        raw_signal=args.raw,
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

    if args.post_analysis == "yes" and args.enable_logging:
        results = analyze_latest_ring_logs(log_dir=args.log_dir, latest_n=2)
        console.print(format_analysis_report(results))
    return 0


def ring_analyzer() -> int:
    """Entry point for nuanic-ring-analyzer command."""
    from nuanic_ring.data_analysis import print_export_fit_report, print_report

    parser = argparse.ArgumentParser(description="Analyze ring CSV log files")
    parser.add_argument("filepath", help="Path to CSV log file")
    parser.add_argument(
        "--fit-mm", action="store_true", help="Attempt MM-like equation fit"
    )
    args = parser.parse_args()

    filepath = Path(args.filepath)
    if not filepath.exists():
        print(f"[ERROR] File not found: {filepath}")
        return 1

    try:
        if args.fit_mm and print_export_fit_report(str(filepath)):
            return 0
        print_report(str(filepath))
        return 0
    except Exception as e:
        print(f"[ERROR] Analysis failed: {e}")
        return 1


def ring_post_analysis() -> int:
    """Entry point for nuanic-ring-post-analysis command."""
    parser = argparse.ArgumentParser(description="Analyze latest ring CSV logs")
    parser.add_argument("--log-dir", default="data/ring_logs")
    parser.add_argument("--latest", type=int, default=2)
    args = parser.parse_args()

    results = analyze_latest_ring_logs(log_dir=args.log_dir, latest_n=args.latest)
    print(format_analysis_report(results))
    return 0

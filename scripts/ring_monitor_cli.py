#!/usr/bin/env python3
"""Multi-ring monitor CLI with Rich live dashboard (wrapper)."""

from nuanic_ring.cli import (
    _NonBlockingLineReader,
    _build_dashboard_renderable,
    _build_dashboard_table,
    _build_marker_hotkeys,
    _default_marker_hotkeys,
    _format_marker_legend,
    _parse_marker_hotkey_spec,
    _parse_marker_label,
    _poll_marker_input,
    _run_monitor_cli,
    build_parser,
)
import asyncio
import sys


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(_run_monitor_cli(args))


if __name__ == "__main__":
    sys.exit(main())

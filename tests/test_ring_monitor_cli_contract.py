"""Verify CLI argument parser defaults and marker hotkey handling."""

from nuanic_ring.cli import _build_marker_hotkeys, build_parser


def test_cli_defaults_match_documented_values():
    args = build_parser().parse_args([])

    assert args.stagger_delay == 1.25
    assert args.ui_refresh_ms == 200
    assert args.rate_control == "yes"
    assert args.equalize_mode == "log-only"
    assert args.csv_layout == "combined"
    assert args.scan_timeout == 6.0
    assert args.scan_attempts == 3
    assert args.warmup_delay == 3.0
    assert args.markers is False


def test_cli_default_marker_hotkeys():
    hotkeys = _build_marker_hotkeys([])

    assert hotkeys["SPACE"] == "marker"
    assert hotkeys["S"] == "stimulus_on"
    assert hotkeys["B"] == "baseline_start"
    assert hotkeys["R"] == "rest_start"


def test_cli_marker_hotkey_override():
    hotkeys = _build_marker_hotkeys(["S=start", "X=cleanup"])

    assert hotkeys["S"] == "start"
    assert hotkeys["X"] == "cleanup"

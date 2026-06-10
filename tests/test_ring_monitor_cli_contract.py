import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


def _ring_monitor_cli():
    return import_module("ring_monitor_cli")


def test_cli_defaults_match_documented_values():
    parser = _ring_monitor_cli().build_parser()
    args = parser.parse_args([])

    assert args.stagger_delay == 1.25
    assert args.ui_refresh_ms == 200
    assert args.rate_control == "yes"
    assert args.equalize_mode == "log-only"
    assert args.csv_layout == "combined"
    assert args.scan_timeout == 6.0
    assert args.scan_attempts == 3
    assert args.warmup_delay == 3.0
    assert args.markers is False


def test_cli_backward_compat_alias_for_post_analysis():
    parser = _ring_monitor_cli().build_parser()
    args = parser.parse_args(["--posanalysys", "yes"])
    assert args.post_analysis == "yes"


def test_cli_default_marker_hotkeys():
    hotkeys = _ring_monitor_cli()._build_marker_hotkeys([])

    assert hotkeys["SPACE"] == "marker"
    assert hotkeys["S"] == "stimulus_on"
    assert hotkeys["B"] == "baseline_start"
    assert hotkeys["R"] == "rest_start"


def test_cli_marker_hotkey_override():
    hotkeys = _ring_monitor_cli()._build_marker_hotkeys(["S=start", "X=cleanup"])

    assert hotkeys["S"] == "start"
    assert hotkeys["X"] == "cleanup"

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from ring_monitor_cli import build_parser


def test_cli_defaults_match_documented_values():
    parser = build_parser()
    args = parser.parse_args([])

    assert args.stagger_delay == 1.25
    assert args.ui_refresh_ms == 200
    assert args.rate_control == "yes"
    assert args.equalize_mode == "log-only"
    assert args.scan_timeout == 6.0
    assert args.scan_attempts == 3
    assert args.warmup_delay == 3.0


def test_cli_backward_compat_alias_for_post_analysis():
    parser = build_parser()
    args = parser.parse_args(["--posanalysys", "yes"])
    assert args.post_analysis == "yes"

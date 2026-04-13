"""Console entrypoint launchers for CLI scripts.

These wrappers let users run installed commands (e.g. ``nuanic-ring-monitor``)
while preserving the existing script implementations.
"""

from __future__ import annotations

import runpy
from pathlib import Path


def _script_path(filename: str) -> Path:
    # Editable installs point to the repository source tree.
    candidate = Path(__file__).resolve().parents[2] / "scripts" / filename
    if not candidate.exists():
        raise FileNotFoundError(
            f"CLI script not found: {candidate}. "
            "Reinstall in editable mode from repository root: pip install -e .[dev]"
        )
    return candidate


def _run_script(filename: str) -> int:
    script = _script_path(filename)
    runpy.run_path(str(script), run_name="__main__")
    return 0


def ring_monitor() -> int:
    return _run_script("ring_monitor_cli.py")


def ring_analyzer() -> int:
    return _run_script("ring_analyzer_cli.py")


def ring_post_analysis() -> int:
    return _run_script("ring_post_analysis_cli.py")


def ring_discover_services() -> int:
    return _run_script("discover_ring_services.py")

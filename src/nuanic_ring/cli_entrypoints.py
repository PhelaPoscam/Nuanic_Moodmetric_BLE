"""Console entrypoint launchers for CLI commands.

These entrypoints are wired to ``pyproject.toml`` ``[project.scripts]`` and
work from both editable installs (``pip install -e .``) and regular wheel
installs (``pip install nuanic-ring``).
"""

from __future__ import annotations

from nuanic_ring.cli import ring_analyzer, ring_monitor, ring_post_analysis
from nuanic_ring.discover_services import main as ring_discover_services

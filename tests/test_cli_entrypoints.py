"""Verify that CLI entrypoints resolve to callable functions.

The entrypoints are wired in ``pyproject.toml`` ``[project.scripts]`` and must
be importable + callable regardless of whether the package is installed from a
wheel or in editable mode.
"""

from nuanic_ring import cli, discover_services


def test_ring_monitor_entrypoint_is_callable():
    assert callable(cli.ring_monitor)


def test_ring_analyzer_entrypoint_is_callable():
    assert callable(cli.ring_analyzer)


def test_ring_post_analysis_entrypoint_is_callable():
    assert callable(cli.ring_post_analysis)


def test_ring_discover_services_entrypoint_is_callable():
    assert callable(discover_services.main)

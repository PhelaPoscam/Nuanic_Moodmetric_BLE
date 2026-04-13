import inspect

from nuanic_ring.connector import NuanicConnector
from nuanic_ring.monitor import NuanicMonitor


def test_connector_scan_default_contracts():
    list_sig = inspect.signature(NuanicConnector.list_available_rings)
    discover_sig = inspect.signature(NuanicConnector.discover_all_matching_rings)

    assert list_sig.parameters["scan_timeout"].default == 6.0
    assert list_sig.parameters["attempts"].default == 3
    assert discover_sig.parameters["scan_timeout"].default == 6.0
    assert discover_sig.parameters["attempts"].default == 3


def test_monitor_start_multi_scan_args_contract():
    sig = inspect.signature(NuanicMonitor.start_multi)

    assert "scan_timeout" in sig.parameters
    assert "scan_attempts" in sig.parameters
    assert sig.parameters["scan_timeout"].default is None
    assert sig.parameters["scan_attempts"].default is None

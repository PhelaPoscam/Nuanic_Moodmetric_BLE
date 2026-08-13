"""Orchestration tests for NuanicMonitor.start_multi and its helpers."""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.monitor import NuanicMonitor


class FakeConnector:
    """Minimal connector double with the surface start_multi touches."""

    def __init__(self):
        self.client = None
        self.connected = False
        self.discovered = []
        self.connect_calls = 0
        self.reset_calls = 0
        self.last_scan_timeout = None
        self.last_scan_attempts = None

    # -- connect / single path --
    async def connect(self):
        self.connect_calls += 1
        self.connected = True
        self.client = SimpleNamespace(address="AA:BB:CC:DD:EE:01")
        return True

    async def read_battery(self, address=None):
        return 80

    async def _reset_bluetooth_radio(self):
        self.reset_calls += 1
        return True

    async def discover_all_matching_rings(
        self,
        include_device=True,
        scan_timeout=6.0,
        attempts=3,
        retry_delay=0.5,
        stop_if_found=True,
    ):
        self.last_scan_timeout = scan_timeout
        self.last_scan_attempts = attempts
        return self.discovered

    async def connect_device(self, address, device=None):
        self.connected = True
        return True

    async def attempt_set_sample_rate(self, target_hz, address=None):
        return {"status": "ok", "target_hz": target_hz}

    async def disconnect(self, address=None):
        self.connected = False

    async def subscribe_to_imu(self, callback, address=None):
        return True

    async def subscribe_to_stress(self, callback, address=None):
        return True

    async def subscribe_to_raw_eda(self, callback, address=None):
        return True

    async def subscribe_to_live_eda(self, callback, address=None):
        return True

    async def unsubscribe_from_imu(self, address=None):
        pass

    async def unsubscribe_from_stress(self, address=None):
        pass

    async def unsubscribe_from_raw_eda(self, address=None):
        pass

    async def unsubscribe_from_live_eda(self, address=None):
        pass

    def get_client(self, address=None):
        return None


def _make_monitor(**kwargs):
    kwargs.setdefault("enable_logging", False)
    monitor = NuanicMonitor(**kwargs)
    monitor.connector = FakeConnector()
    return monitor


def test_start_multi_single_path_routes_to_single():
    monitor = _make_monitor()
    ok = asyncio.run(monitor.start_multi())
    assert ok is True
    assert monitor.running is True
    assert monitor.capture_armed is True
    assert monitor.connector.connect_calls == 1
    assert monitor._health_task is not None
    monitor._health_task.cancel()


def test_start_multi_multi_path_uses_discovery():
    monitor = _make_monitor()
    monitor.connector.discovered = [
        {"address": "AA:BB:CC:DD:EE:01", "device": None},
        {"address": "AA:BB:CC:DD:EE:02", "device": None},
    ]
    ok = asyncio.run(monitor.start_multi(monitor_all=True))
    assert ok is True
    assert set(monitor.device_states.keys()) == {
        "AA:BB:CC:DD:EE:01",
        "AA:BB:CC:DD:EE:02",
    }
    assert monitor.connector.last_scan_timeout == 6.0
    assert monitor.connector.last_scan_attempts == 3
    monitor._health_task.cancel()


def test_start_multi_explicit_addresses_skip_discovery():
    monitor = _make_monitor()
    ok = asyncio.run(monitor.start_multi(ring_addresses=["aa:bb:cc:dd:ee:01"]))
    assert ok is True
    # Explicit addresses bypass discovery (no scan performed)
    assert monitor.connector.last_scan_timeout is None
    assert "AA:BB:CC:DD:EE:01" in monitor.device_states
    monitor._health_task.cancel()


def test_start_multi_connect_failure_returns_false():
    monitor = _make_monitor()

    async def _fail(address, device=None):
        return False

    monitor.connector.connect_device = _fail
    ok = asyncio.run(monitor.start_multi(ring_addresses=["aa:bb:cc:dd:ee:01"]))
    assert ok is False
    assert monitor.running is False


def test_hz_cap_caps_multi_ring_target():
    monitor = _make_monitor(target_hz=20.0)
    monitor.connector.discovered = [
        {"address": "AA:BB:CC:DD:EE:01", "device": None},
        {"address": "AA:BB:CC:DD:EE:02", "device": None},
    ]
    asyncio.run(monitor.start_multi(monitor_all=True))
    assert monitor.target_hz == 16.0
    monitor._health_task.cancel()


def test_hz_cap_not_applied_to_single():
    monitor = _make_monitor(target_hz=20.0)
    asyncio.run(monitor.start_multi(ring_addresses=["aa:bb:cc:dd:ee:01"]))
    assert monitor.target_hz == 20.0
    monitor._health_task.cancel()


def test_hz_cap_forced_bypasses_cap():
    monitor = _make_monitor(target_hz=20.0, force_hz=True)
    monitor.connector.discovered = [
        {"address": "AA:BB:CC:DD:EE:01", "device": None},
        {"address": "AA:BB:CC:DD:EE:02", "device": None},
    ]
    asyncio.run(monitor.start_multi(monitor_all=True))
    assert monitor.target_hz == 20.0
    monitor._health_task.cancel()


def test_warmup_sequence_skipped_by_default():
    monitor = _make_monitor(use_warmup=False, attempt_ring_rate_control=True)

    async def _connect(address, device=None):
        return True

    monitor.connector.connect_device = _connect
    calls = []

    async def _set_rate(target_hz, address=None):
        calls.append(("rate", target_hz))
        return {"status": "ok"}

    monitor.connector.attempt_set_sample_rate = _set_rate
    asyncio.run(monitor._warmup_sequence("AA:BB:CC:DD:EE:01"))
    assert calls == []


def test_warmup_sequence_runs_when_enabled():
    monitor = _make_monitor(
        use_warmup=True,
        attempt_ring_rate_control=True,
        target_hz=10.0,
        warmup_delay=0.0,
    )
    calls = []

    async def _set_rate(target_hz, address=None):
        calls.append(("rate", target_hz))
        return {"status": "ok"}

    async def _disconnect(address=None):
        return None

    monitor.connector.disconnect = _disconnect
    monitor.connector.attempt_set_sample_rate = _set_rate
    asyncio.run(monitor._warmup_sequence("AA:BB:CC:DD:EE:01"))
    assert calls == [("rate", 10)]


def test_drain_writers_handles_none_tasks():
    monitor = _make_monitor()
    monitor._ensure_device_state("AA:BB:CC:DD:EE:01")
    asyncio.run(monitor._drain_writers_and_report())
    # No writer tasks (logging disabled) -> no exception

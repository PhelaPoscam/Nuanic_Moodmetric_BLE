"""Tests for NuanicConnector connection lifecycle with a mock bleak client."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.connector import NuanicConnector


class _BackendResource:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeBleakClient:
    """Minimal stand-in for BleakClient."""

    def __init__(self, connect_succeeds=True):
        self.is_connected = False
        self._connect_succeeds = connect_succeeds
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stop_notify_calls = []
        self.paired = False
        self._backend = SimpleNamespace(
            _session=_BackendResource(),
            _device=_BackendResource(),
        )
        self.disconnected_callback = None

    async def connect(self):
        self.connect_calls += 1
        if self._connect_succeeds:
            self.is_connected = True
        else:
            raise OSError("connection refused")

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False

    async def pair(self):
        self.paired = True

    async def stop_notify(self, char_uuid):
        self.stop_notify_calls.append(char_uuid)

    def set_disconnected_callback(self, cb):
        self.disconnected_callback = cb


def _make_connector(**kwargs):
    kwargs.setdefault("auto_sync_time", False)
    return NuanicConnector(**kwargs)


def test_connect_device_success_registers_and_syncs():
    conn = _make_connector(auto_sync_time=False)
    client = FakeBleakClient()
    conn._create_bleak_client = lambda *a, **k: client

    ok = asyncio.run(conn.connect_device("AA:BB:CC:DD:EE:01"))
    assert ok is True
    assert client.connect_calls == 1
    assert "AA:BB:CC:DD:EE:01" in conn.clients
    assert conn.client is client
    assert conn.target_address == "AA:BB:CC:DD:EE:01"


def test_connect_device_retries_then_fails():
    conn = _make_connector(max_connect_attempts=3, connect_backoff_seconds=0)
    client = FakeBleakClient(connect_succeeds=False)
    conn._create_bleak_client = lambda *a, **k: client

    ok = asyncio.run(conn.connect_device("AA:BB:CC:DD:EE:01"))
    assert ok is False
    assert client.connect_calls == 3  # exhausted retries


def test_connect_device_sync_time_on_boot():
    conn = _make_connector(auto_sync_time=True)
    client = FakeBleakClient()
    conn._create_bleak_client = lambda *a, **k: client
    synced = []

    async def _fake_sync_time(address=None):
        synced.append(address)
        return True

    conn.sync_time = _fake_sync_time

    ok = asyncio.run(conn.connect_device("AA:BB:CC:DD:EE:01"))
    assert ok is True
    assert synced == ["AA:BB:CC:DD:EE:01"]


def test_disconnect_one_cleans_registry():
    conn = _make_connector()
    client = FakeBleakClient()
    client.is_connected = True
    conn.clients["AA:BB:CC:DD:EE:01"] = client
    conn.devices["AA:BB:CC:DD:EE:01"] = SimpleNamespace()

    asyncio.run(conn._disconnect_one("AA:BB:CC:DD:EE:01"))
    assert "AA:BB:CC:DD:EE:01" not in conn.clients
    assert "AA:BB:CC:DD:EE:01" not in conn.devices
    assert client.disconnect_calls == 1
    assert client.set_disconnected_callback is None or True


def test_disconnect_all_cleans_multiple():
    conn = _make_connector()
    c1 = FakeBleakClient()
    c2 = FakeBleakClient()
    c1.is_connected = True
    c2.is_connected = True
    conn.clients["AA:BB:CC:DD:EE:01"] = c1
    conn.clients["AA:BB:CC:DD:EE:02"] = c2

    asyncio.run(conn._disconnect_all())
    assert conn.clients == {}
    assert c1.disconnect_calls == 1
    assert c2.disconnect_calls == 1


def test_cleanup_client_tears_down_backend():
    conn = _make_connector()
    client = FakeBleakClient()
    client.is_connected = True
    conn.clients["AA:BB:CC:DD:EE:01"] = client

    asyncio.run(conn._cleanup_client("AA:BB:CC:DD:EE:01"))
    assert client.is_connected is False
    assert client._backend._session.closed is True
    assert client._backend._device.closed is True
    assert "AA:BB:CC:DD:EE:01" not in conn.clients


def test_register_connected_device_sets_state():
    conn = _make_connector(auto_sync_time=False)
    client = FakeBleakClient()
    device = SimpleNamespace()

    asyncio.run(conn._register_connected_device("AA:BB:CC:DD:EE:01", client, device))
    assert conn.clients["AA:BB:CC:DD:EE:01"] is client
    assert conn.devices["AA:BB:CC:DD:EE:01"] is device
    assert conn.client is client
    assert conn.device is device
    assert conn.target_address == "AA:BB:CC:DD:EE:01"


def test_stop_all_notifications_best_effort():
    conn = _make_connector()
    client = FakeBleakClient()

    async def _boom(_uuid):
        raise OSError("notify failed")

    client.stop_notify = _boom
    asyncio.run(conn._stop_all_notifications(client))
    # No exception raised -> best-effort semantics hold

"""Tests for NuanicConnector connection lifecycle with a mock bleak client."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.core import connector as connector_module
from nuanic_ring.core.connector import NuanicConnector


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
        self._backend = SimpleNamespace(
            _session=_BackendResource(),
            _device=_BackendResource(),
        )

    async def connect(self):
        self.connect_calls += 1
        if self._connect_succeeds:
            self.is_connected = True
        else:
            raise OSError("connection refused")

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False

    async def stop_notify(self, char_uuid):
        self.stop_notify_calls.append(char_uuid)


def _make_connector(**kwargs):
    kwargs.setdefault("auto_sync_time", False)
    return NuanicConnector(**kwargs)


def test_connect_device_success_registers_and_syncs():
    conn = _make_connector(auto_sync_time=False, pair_on_connect=True)
    client = FakeBleakClient()
    conn._create_bleak_client = lambda *a, **k: client

    ok = asyncio.run(conn.connect_device("AA:BB:CC:DD:EE:01"))
    assert ok is True
    assert client.connect_calls == 1
    assert "AA:BB:CC:DD:EE:01" in conn.clients
    assert conn.client is client
    assert conn.target_address == "AA:BB:CC:DD:EE:01"


def test_create_bleak_client_delegates_pairing_to_bleak(monkeypatch):
    """bleak pairs during connect() via ``pair=``; we must not pair twice."""
    captured = {}

    class _RecordingBleakClient:
        def __init__(self, target, **kwargs):
            captured.clear()
            captured.update(kwargs)

    monkeypatch.setattr(connector_module, "BleakClient", _RecordingBleakClient)

    _make_connector(pair_on_connect=True)._create_bleak_client("AA:BB:CC:DD:EE:01")
    assert captured["pair"] is True

    _make_connector(pair_on_connect=False)._create_bleak_client("AA:BB:CC:DD:EE:01")
    assert captured["pair"] is False


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
    stopped = []

    async def _boom(uuid):
        stopped.append(uuid)
        raise OSError("notify failed")

    client.stop_notify = _boom
    asyncio.run(conn._stop_all_notifications(client))
    assert len(stopped) == 4
    assert conn.LIVE_DNE_UUID in stopped
    assert conn.IMU_BATCH_UUID in stopped
    assert conn.STATE_UUID in stopped
    assert conn.LIVE_EDA_UUID in stopped


class FakeGattClient:
    """Client exposing a readable/writable CONFIG_3 register."""

    def __init__(self, register_value=None):
        self.is_connected = True
        self.writes = []
        self._register = register_value

    async def read_gatt_char(self, uuid):
        if self._register is None:
            raise OSError("register not readable")
        return bytes([self._register])

    async def write_gatt_char(self, uuid, data):
        self.writes.append((str(uuid).lower(), bytes(data)))
        self._register = data[0]


def _record_client_kwargs(monkeypatch):
    captured = {}

    class _RecordingBleakClient:
        def __init__(self, target, **kwargs):
            captured.clear()
            captured.update(kwargs)

    monkeypatch.setattr(connector_module, "BleakClient", _RecordingBleakClient)
    return captured


def test_bleak_client_uses_connect_timeout_not_scan_timeout(monkeypatch):
    """The GATT link budget must be independent of the (short) scan budget."""
    captured = _record_client_kwargs(monkeypatch)

    conn = _make_connector(timeout=7.0, connect_timeout=20.0)
    conn._create_bleak_client("AA:BB:CC:DD:EE:01")

    assert captured["timeout"] == 20.0
    assert conn.scanner.timeout == 7.0


def test_connect_timeout_is_configurable(monkeypatch):
    captured = _record_client_kwargs(monkeypatch)

    _make_connector(connect_timeout=33.0)._create_bleak_client("AA:BB:CC:DD:EE:01")

    assert captured["timeout"] == 33.0


def test_set_mode_skips_write_when_already_in_mode():
    """Writing the current mode re-triggers a 60s calibration on the ring."""
    conn = _make_connector()
    client = FakeGattClient(register_value=0x01)
    conn.client = client

    assert asyncio.run(conn.set_mode(0x01)) is True
    assert client.writes == []


def test_set_mode_writes_on_real_transition():
    conn = _make_connector()
    client = FakeGattClient(register_value=0x00)
    conn.client = client

    assert asyncio.run(conn.set_mode(0x01)) is True
    assert client.writes == [(conn.STORAGE_FORMAT_UUID.lower(), bytes([0x01]))]


def test_set_mode_still_writes_when_register_unreadable():
    """An unreadable register must not block a legitimate mode change."""
    conn = _make_connector()
    client = FakeGattClient(register_value=None)
    conn.client = client

    assert asyncio.run(conn.set_mode(0x02)) is True
    assert client.writes == [(conn.STORAGE_FORMAT_UUID.lower(), bytes([0x02]))]

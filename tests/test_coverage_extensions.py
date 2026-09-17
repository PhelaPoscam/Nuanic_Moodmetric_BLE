"""Direct unit tests covering previously untested production features and edge cases.

Includes:
- get_smoothed_time and nuanic_ts_fields (telemetry/time_sync.py)
- _connection_health_loop (monitor.py)
- _parse_storage_record and download_storage (core/connector.py)
- sync_time, attempt_set_sample_rate, set_mode, read_storage_usage, rewind_storage
- SignalConditioner Nyquist limit protection and edge cases (dsp/signal_processing.py)
"""

import asyncio
import math
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.core.connector import NuanicConnector
from nuanic_ring.dsp.signal_processing import SignalConditioner
from nuanic_ring.monitor import NuanicMonitor
from nuanic_ring.telemetry.device_state import RingDeviceState
from nuanic_ring.telemetry.time_sync import get_smoothed_time, nuanic_ts_fields

# ---------------------------------------------------------------------------
# 1. Telemetry time sync & clock reconstruction
# ---------------------------------------------------------------------------


def test_get_smoothed_time_initializes_anchor_and_offset():
    state = RingDeviceState(mac="AA:BB:CC:DD:EE:01")
    assert state.d306_ts_anchor is None
    assert state.d306_clock_offset is None

    ts1, elapsed1 = get_smoothed_time(state, "d306", 1000)
    assert state.d306_ts_anchor is not None
    assert state.d306_clock_offset == 1000
    assert elapsed1 == 1

    # Subsequent packet: anchor stays the same, timestamp advances exactly by delta ms
    ts2, elapsed2 = get_smoothed_time(state, "d306", 1250)
    assert state.d306_clock_offset == 1000
    assert int((ts2 - ts1).total_seconds() * 1000) == 250
    assert elapsed2 == 250


def test_get_smoothed_time_clock_rollback_resets_anchor():
    state = RingDeviceState(mac="AA:BB:CC:DD:EE:01")
    get_smoothed_time(state, "d306", 5000)
    assert state.d306_clock_offset == 5000

    # Clock rolled back (e.g. ring rebooted or counter reset)
    _, _ = get_smoothed_time(state, "d306", 100)
    assert state.d306_clock_offset == 100


def test_get_smoothed_time_imu_independent_anchor():
    state = RingDeviceState(mac="AA:BB:CC:DD:EE:01")
    get_smoothed_time(state, "d306", 5000)
    get_smoothed_time(state, "imu", 1000)

    assert state.d306_clock_offset == 5000
    assert state.imu_clock_offset == 1000


def test_get_smoothed_time_with_session_start_time():
    state = RingDeviceState(mac="AA:BB:CC:DD:EE:01")
    start = datetime.now() - timedelta(seconds=10)
    ts, elapsed = get_smoothed_time(state, "d306", 1000, start_time=start)
    assert elapsed >= 10000


def test_nuanic_ts_fields():
    fixed = datetime(2026, 7, 3, 12, 0, 0, 123456, tzinfo=timezone.utc)
    unix_str, utc_str = nuanic_ts_fields(fixed)
    assert unix_str.startswith("17830")
    assert "2026-07-03 12:00:00.123+00" in utc_str


# ---------------------------------------------------------------------------
# 2. Connection health loop logic
# ---------------------------------------------------------------------------


def test_health_loop_keeps_connected_status():
    monitor = NuanicMonitor(enable_logging=False)
    state = monitor._ensure_device_state("AA:BB:CC:DD:EE:01")
    state.status = "reconnecting"

    client = SimpleNamespace(is_connected=True)
    monitor.connector.get_client = lambda mac: client
    monitor.running = True

    async def _run_one_cycle():
        # Run health loop for a short moment then cancel
        task = asyncio.create_task(monitor._connection_health_loop())
        await asyncio.sleep(0.05)
        monitor.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run_one_cycle())
    assert state.status == "connected"


def test_health_loop_marks_offline_when_reconnect_disabled():
    monitor = NuanicMonitor(enable_logging=False)
    monitor._auto_reconnect = False
    state = monitor._ensure_device_state("AA:BB:CC:DD:EE:01")
    state.status = "connected"

    client = SimpleNamespace(is_connected=False)
    monitor.connector.get_client = lambda mac: client
    monitor.running = True

    async def _run_one_cycle():
        task = asyncio.create_task(monitor._connection_health_loop())
        await asyncio.sleep(0.05)
        monitor.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run_one_cycle())
    assert state.status == "offline"


def test_health_loop_empty_device_states_no_unbound_error():
    monitor = NuanicMonitor(enable_logging=False)
    monitor.device_states.clear()
    monitor.running = True

    async def _run_one_cycle():
        task = asyncio.create_task(monitor._connection_health_loop())
        await asyncio.sleep(0.05)
        monitor.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Should not raise UnboundLocalError
    asyncio.run(_run_one_cycle())


# ---------------------------------------------------------------------------
# 3. Storage record parsing and download
# ---------------------------------------------------------------------------


def test_parse_storage_record_format_1_valid():
    conn = NuanicConnector()
    payload = struct.pack("<HQI", 5, 1720000000000, 500000)
    parsed = conn._parse_storage_record(payload, format_type=1)
    assert parsed is not None
    assert parsed["format"] == "EDA"
    assert parsed["boot_count"] == 5
    assert parsed["timestamp_ms"] == 1720000000000
    assert parsed["eda_ohm"] == 500000
    assert parsed["resistance_kohm"] == 500.0
    assert parsed["conductance_us"] == 2.0


def test_parse_storage_record_format_1_truncated():
    conn = NuanicConnector()
    assert conn._parse_storage_record(b"\x00" * 13, format_type=1) is None
    assert conn._parse_storage_record(b"", format_type=1) is None


def test_parse_storage_record_format_2_valid():
    conn = NuanicConnector()
    payload = struct.pack("<HQiii", 3, 1720000000000, 10, 20, 35)
    parsed = conn._parse_storage_record(payload, format_type=2)
    assert parsed is not None
    assert parsed["format"] == "DNE"
    assert parsed["boot_count"] == 3
    assert parsed["timestamp_ms"] == 1720000000000
    assert parsed["srrn"] == 10
    assert parsed["srl"] == 20
    assert parsed["dne"] == 35


def test_parse_storage_record_format_2_truncated():
    conn = NuanicConnector()
    assert conn._parse_storage_record(b"\x00" * 21, format_type=2) is None


def test_parse_storage_record_unknown_format():
    conn = NuanicConnector()
    assert conn._parse_storage_record(b"\x00" * 30, format_type=99) is None


def test_download_storage_chunks():
    conn = NuanicConnector()
    record1 = struct.pack("<HQI", 1, 1000, 250000)
    record2 = struct.pack("<HQI", 1, 2000, 500000)
    combined = record1 + record2

    # Simulate chunked delivery across 2 reads, followed by empty read
    chunks = [combined[:20], combined[20:], b""]

    async def _mock_read_buffer(address=None):
        return chunks.pop(0)

    conn.read_buffer = _mock_read_buffer
    records = asyncio.run(conn.download_storage(format_type=1))
    assert len(records) == 2
    assert records[0]["timestamp_ms"] == 1000
    assert records[1]["timestamp_ms"] == 2000


# ---------------------------------------------------------------------------
# 4. Connector control and diagnostic endpoints
# ---------------------------------------------------------------------------


class MockGattClient:
    def __init__(self):
        self.is_connected = True
        self.writes = []
        self.reads = {}

    async def write_gatt_char(self, uuid, data):
        self.writes.append((str(uuid).lower(), bytes(data)))

    async def read_gatt_char(self, uuid):
        return self.reads.get(str(uuid).lower(), b"")


def test_sync_time():
    conn = NuanicConnector()
    client = MockGattClient()
    conn.client = client

    ok = asyncio.run(conn.sync_time(timestamp_ms=1720000000123))
    assert ok is True
    assert len(client.writes) == 1
    uuid, payload = client.writes[0]
    assert uuid == conn.REALTIME_UUID.lower()
    assert struct.unpack("<Q", payload)[0] == 1720000000123


def test_sync_time_disconnected():
    conn = NuanicConnector()
    assert asyncio.run(conn.sync_time()) is False


def test_set_mode():
    conn = NuanicConnector()
    client = MockGattClient()
    conn.client = client

    ok = asyncio.run(conn.set_mode(0x02))
    assert ok is True
    assert len(client.writes) == 1
    uuid, payload = client.writes[0]
    assert uuid == conn.STORAGE_FORMAT_UUID.lower()
    assert payload == bytes([0x02])


def test_set_sample_rate():
    conn = NuanicConnector()
    client = MockGattClient()
    conn.client = client

    ok = asyncio.run(conn.set_sample_rate(16))
    assert ok is True
    assert client.writes[0] == (conn.SAMPLE_RATE_UUID.lower(), bytes([16]))


def test_read_storage_usage():
    conn = NuanicConnector()
    client = MockGattClient()
    client.reads[conn.STORAGE_USAGE_UUID.lower()] = struct.pack("<II", 10000, 2500)
    conn.client = client

    usage = asyncio.run(conn.read_storage_usage())
    assert usage is not None
    assert usage["size_bytes"] == 10000
    assert usage["used_bytes"] == 2500
    assert usage["available_bytes"] == 7500
    assert usage["percent_used"] == 25.0


def test_rewind_storage():
    conn = NuanicConnector()
    client = MockGattClient()
    conn.client = client

    ok = asyncio.run(conn.rewind_storage(boot_count=5, timestamp_ms=5000))
    assert ok is True
    uuid, payload = client.writes[0]
    assert uuid == conn.STORAGE_REWIND_UUID.lower()
    boot, ts = struct.unpack("<HQ", payload)
    assert boot == 5
    assert ts == 5000


# ---------------------------------------------------------------------------
# 5. Signal conditioner Nyquist limit protection
# ---------------------------------------------------------------------------


def test_signal_conditioner_at_3hz_sample_rate():
    """Verify SignalConditioner instantiates and filters at 3 Hz without Nyquist crash."""
    sc = SignalConditioner(sample_rate=3.0, cutoff_hz=1.5)
    out1 = sc.process(10.0)
    out2 = sc.process(12.0)
    assert not math.isnan(out1)
    assert not math.isnan(out2)


def test_signal_conditioner_at_sub_1hz_sample_rate():
    """Verify SignalConditioner handles low sample rates (e.g. 1 Hz, 0.5 Hz)."""
    sc = SignalConditioner(sample_rate=0.5, cutoff_hz=1.5)
    out = sc.process(5.0)
    assert not math.isnan(out)


def test_signal_conditioner_nan_resets():
    sc = SignalConditioner(sample_rate=25.0)
    sc.process(10.0)
    out_nan = sc.process(float("nan"))
    assert math.isnan(out_nan)
    assert sc.z is None
    assert len(sc.median_buffer) == 0

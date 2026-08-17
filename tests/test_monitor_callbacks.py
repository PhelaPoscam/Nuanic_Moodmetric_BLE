"""Tests for monitor packet parsing and BLE callback hot paths."""

import asyncio
import struct
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.monitor import NuanicMonitor


class FakeConnector:
    def __init__(self):
        self.client = None

    async def connect(self):
        self.client = SimpleNamespace(address="AA:BB:CC:DD:EE:01")
        return True

    async def read_battery(self, address=None):
        return 80

    async def connect_device(self, address, device=None):
        return True

    async def attempt_set_sample_rate(self, target_hz, address=None):
        return {"status": "ok", "target_hz": target_hz}

    async def disconnect(self, address=None):
        pass

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


# ── Packet parsing ──────────────────────────────────────────────


def test_parse_d306_packet_valid():
    monitor = _make_monitor()
    ts_ms = 1700000000000
    instant = 42000
    dne = 75
    data = struct.pack("<Qii", ts_ms, instant, dne)
    result = monitor._parse_d306_packet(data)
    assert result is not None
    assert result["timestamp_ms"] == ts_ms
    assert result["instant"] == instant
    assert result["dne"] == dne
    assert result["eda_value"] == instant
    assert result["dne_stress_index"] == dne


def test_parse_d306_packet_wrong_length():
    monitor = _make_monitor()
    assert monitor._parse_d306_packet(b"\x00" * 10) is None
    assert monitor._parse_d306_packet(b"") is None


def test_parse_imu_batch_valid():
    monitor = _make_monitor()
    clock = 12345
    context = 67890
    header = struct.pack("<II", clock, context)
    samples = b""
    for i in range(14):
        samples += struct.pack("<hhh", i * 10, i * 20, i * 30)
    data = header + samples
    assert len(data) == 92
    result = monitor._parse_468f_imu_batch(data)
    assert result is not None
    assert result["clock"] == clock
    assert result["context"] == context
    assert len(result["samples"]) == 14
    assert result["first_x"] == 0
    assert result["first_y"] == 0
    assert result["first_z"] == 0


def test_parse_imu_batch_wrong_length():
    monitor = _make_monitor()
    assert monitor._parse_468f_imu_batch(b"\x00" * 10) is None


# ── Callback state updates ──────────────────────────────────────

MAC = "AA:BB:CC:DD:EE:01"


def _armed_monitor(**kwargs):
    monitor = _make_monitor(**kwargs)
    monitor.start_time = datetime.now()
    monitor.running = True
    monitor.capture_armed = True
    return monitor


def test_stress_callback_updates_state():
    monitor = _armed_monitor()
    cb = monitor._make_stress_callback(MAC)
    ts_ms = 1700000000000
    instant = 42000
    dne = 75
    data = struct.pack("<Qii", ts_ms, instant, dne)
    cb(None, data)

    state = monitor.device_states[MAC]
    assert state.raw_eda == instant
    assert state.dne_stress_index == dne
    assert state.d306_count == 1
    assert state.last_seen is not None
    assert len(state.d306_buffer) == 1


def test_stress_callback_skips_when_disarmed():
    monitor = _armed_monitor()
    monitor.capture_armed = False
    cb = monitor._make_stress_callback(MAC)
    data = struct.pack("<Qii", 1700000000000, 42000, 75)
    cb(None, data)
    assert MAC not in monitor.device_states


def test_stress_callback_ignores_short_packet():
    monitor = _armed_monitor()
    cb = monitor._make_stress_callback(MAC)
    cb(None, b"\x00" * 4)
    state = monitor.device_states.get(MAC)
    assert state is not None  # state created by _ensure_device_state
    assert state.d306_count == 0  # but packet was rejected


def test_live_eda_callback_updates_state():
    monitor = _armed_monitor()
    cb = monitor._make_live_eda_callback(MAC)
    boot_count = 1
    ts_ms = 1700000000000
    eda_ohm = 50000
    data = struct.pack("<HQI", boot_count, ts_ms, eda_ohm)
    assert len(data) == 14
    cb(None, data)

    state = monitor.device_states[MAC]
    assert state.raw_eda == eda_ohm
    assert state.live_eda_count == 1
    assert state.last_seen is not None
    assert state.filtered_us is not None


def test_live_eda_callback_skips_when_disarmed():
    monitor = _armed_monitor()
    monitor.capture_armed = False
    cb = monitor._make_live_eda_callback(MAC)
    data = struct.pack("<HQI", 1, 1700000000000, 50000)
    cb(None, data)
    assert MAC not in monitor.device_states


def test_imu_callback_updates_state():
    monitor = _armed_monitor()
    cb = monitor._make_imu_callback(MAC)
    header = struct.pack("<II", 100, 200)
    samples = b""
    for i in range(14):
        samples += struct.pack("<hhh", i, i * 2, i * 3)
    data = header + samples
    cb(None, data)

    state = monitor.device_states[MAC]
    assert state.imu_batch_count == 1
    assert state.last_seen is not None
    assert len(state.imu_batch_buffer) == 1


def test_raw_eda_callback_updates_state():
    monitor = _armed_monitor()
    cb = monitor._make_raw_eda_callback(MAC)
    data = bytes([2])  # state_code = 2 (on)
    cb(None, data)

    state = monitor.device_states[MAC]
    assert state.state_count == 1
    assert state.last_seen is not None


def test_stress_callback_logs_to_combined():
    monitor = _armed_monitor(csv_layout="combined", enable_logging=True)
    cb = monitor._make_stress_callback(MAC)
    data = struct.pack("<Qii", 1700000000000, 42000, 75)
    cb(None, data)

    state = monitor.device_states[MAC]
    assert state.log_queue is not None
    assert state.log_queue.qsize() == 1


def test_stress_callback_logs_to_nuanic():
    monitor = _armed_monitor(csv_layout="nuanic", enable_logging=True)
    cb = monitor._make_stress_callback(MAC)
    data = struct.pack("<Qii", 1700000000000, 42000, 75)
    cb(None, data)

    state = monitor.device_states[MAC]
    assert state.log_queue is not None
    row = state.log_queue.get_nowait()
    assert row[0] == MAC  # address field
    assert row[3] == 75  # dne field


def test_stress_callback_logs_to_split():
    monitor = _armed_monitor(csv_layout="split", enable_logging=True)
    cb = monitor._make_stress_callback(MAC)
    data = struct.pack("<Qii", 1700000000000, 42000, 75)
    cb(None, data)

    state = monitor.device_states[MAC]
    assert state.stream_log_queue is not None
    assert state.computed_log_queue is not None
    assert state.stream_log_queue.qsize() == 1
    assert state.computed_log_queue.qsize() == 1

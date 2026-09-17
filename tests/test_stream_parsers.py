"""Unit tests for Nuanic Ring BLE stream parsers and constants."""

import struct

import pytest

from nuanic_ring.core.connector import NuanicConnector
from nuanic_ring.monitor import NuanicMonitor


def test_connector_uuids():
    """Verify that all Nuanic UUID constants are present and formatted correctly."""
    conn = NuanicConnector()
    assert conn.REALTIME_UUID == "dc9c31a7-fbd3-467a-8777-10900c423d3b"
    assert conn.STORAGE_USAGE_UUID == "d78e5bd8-53d6-4fc3-bc98-03b8cd71684b"
    assert conn.STORAGE_REWIND_UUID == "2175c13f-60e4-4de5-80af-0d06f1b54880"
    assert conn.COMMAND_UUID == "741f0d15-cc3d-4715-a9fb-a5a6bccebc50"
    assert conn.STORAGE_FORMAT_UUID == "3cce21a7-e602-4e02-8c52-1e0366c1c846"
    assert conn.STORAGE_UUID == "7c3b82e7-22b7-4cb6-8458-ba325edf6ede"
    assert conn.SAMPLE_RATE_UUID == "516b0fb6-d861-4619-9dd0-0105e8b85128"
    assert conn.LIVE_EDA_UUID == "42dcb71b-1817-43bd-8ea3-7272780a1c9f"
    assert conn.LIVE_DNE_UUID == "d306262b-c8c9-4c4b-9050-3a41dea706e5"

    # Verify canonical UUIDs are properly set
    assert conn.LIVE_DNE_UUID == "d306262b-c8c9-4c4b-9050-3a41dea706e5"
    assert conn.LIVE_EDA_UUID == "42dcb71b-1817-43bd-8ea3-7272780a1c9f"
    assert conn.SAMPLE_RATE_UUID == "516b0fb6-d861-4619-9dd0-0105e8b85128"
    assert conn.STORAGE_FORMAT_UUID == "3cce21a7-e602-4e02-8c52-1e0366c1c846"


def test_parse_d306_packet_struct():
    """Verify that _parse_d306_packet unpacks <Qii (8B timestamp, 4B instant, 4B DNE) correctly."""
    monitor = NuanicMonitor()
    timestamp_ms = 1719999999000
    instant = 1000500
    dne = 45

    payload = struct.pack("<Qii", timestamp_ms, instant, dne)
    parsed = monitor._parse_d306_packet(payload)

    assert parsed is not None
    # Check primary specification keys
    assert parsed["timestamp_ms"] == timestamp_ms
    assert parsed["instant"] == instant
    assert parsed["dne"] == dne

    # Check backward compatibility keys
    assert parsed["clock"] == (timestamp_ms & 0xFFFFFFFF)
    assert parsed["context"] == instant
    assert parsed["eda_value"] == instant
    assert parsed["dne_stress_index"] == dne


def test_parse_d306_invalid_length():
    monitor = NuanicMonitor()
    assert monitor._parse_d306_packet(b"short") is None
    assert monitor._parse_d306_packet(b"toolongpayloadforpacket") is None


from nuanic_ring.io.schemas import FingerStatePacket, LiveEdaPacket
from nuanic_ring.telemetry.callbacks import (
    parse_finger_state_packet,
    parse_live_eda_packet,
)


def test_live_eda_unpacking_logic():
    """Verify standard <HQI unpacking for 14-byte Live EDA packets via production parser."""
    boot_count = 12
    timestamp_ms = 1720000000000
    eda_ohm = 500000  # 500 kOhm -> 2 uS

    payload = struct.pack("<HQI", boot_count, timestamp_ms, eda_ohm)
    assert len(payload) == 14

    # Test dataclass schema parser
    packet = LiveEdaPacket.from_bytes(payload)
    assert packet is not None
    assert packet.boot_count == boot_count
    assert packet.timestamp_ms == timestamp_ms
    assert packet.eda_raw_ohms == eda_ohm

    # Test callback parser function
    parsed = parse_live_eda_packet(payload)
    assert parsed is not None
    assert parsed["boot_count"] == boot_count
    assert parsed["timestamp_ms"] == timestamp_ms
    assert parsed["eda_ohm"] == eda_ohm
    assert parsed["resistance_kohm"] == 500.0
    assert parsed["conductance_us"] == 2.0


def test_live_eda_invalid_length():
    """Verify Live EDA parsing rejects payloads of wrong length."""
    assert parse_live_eda_packet(b"\x00" * 13) is None
    assert parse_live_eda_packet(b"\x00" * 15) is None
    assert LiveEdaPacket.from_bytes(b"") is None


def test_finger_state_parsing():
    """Verify finger state packet decoding (UUID 3c180fcc...)."""
    off_finger = parse_finger_state_packet(b"\x01")
    assert off_finger.state_code == 1
    assert off_finger.payload_hex == "01"

    on_finger = parse_finger_state_packet(b"\x02")
    assert on_finger.state_code == 2
    assert on_finger.payload_hex == "02"

    empty = parse_finger_state_packet(b"")
    assert empty.state_code is None
    assert empty.payload_hex == ""

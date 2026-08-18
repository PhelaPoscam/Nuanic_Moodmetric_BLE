"""Tests for strongly typed schemas and CSV row dataclasses."""

import struct

import pytest

from nuanic_ring.schemas import (
    CombinedLogRow,
    ComputedLogRow,
    D306Packet,
    FingerStatePacket,
    ImuBatchPacket,
    ImuLogRow,
    LiveEdaPacket,
    NuanicExportLogRow,
    StreamLogRow,
)


def test_d306_packet_parsing():
    # 16 bytes: <Qii (timestamp_ms=1000, instant=2500, dne=42)
    raw = struct.pack("<Qii", 1000, 2500, 42)
    packet = D306Packet.from_bytes(raw)
    assert packet is not None
    assert packet.timestamp_ms == 1000
    assert packet.instant == 2500
    assert packet.dne == 42
    assert packet.clock == 1000
    assert packet.context == 2500
    assert packet.eda_value == 2500
    assert packet.dne_stress_index == 42

    # Invalid length
    assert D306Packet.from_bytes(b"\x00" * 15) is None


def test_live_eda_packet_parsing():
    # 14 bytes: <HQI (boot_count=1, timestamp_ms=500000, eda_raw_ohms=150000)
    raw = struct.pack("<HQI", 1, 500000, 150000)
    packet = LiveEdaPacket.from_bytes(raw)
    assert packet is not None
    assert packet.boot_count == 1
    assert packet.timestamp_ms == 500000
    assert packet.eda_raw_ohms == 150000

    # Invalid length
    assert LiveEdaPacket.from_bytes(b"\x00" * 12) is None


def test_imu_batch_packet_parsing():
    # 8-byte header (<II) + 14 * 6-byte samples (<hhh)
    samples = [(10, 20, 30), (-10, -20, -30)]
    sample_bytes = b"".join(struct.pack("<hhh", x, y, z) for x, y, z in samples)
    raw = struct.pack("<II", 100, 200) + sample_bytes

    packet = ImuBatchPacket.from_bytes(raw)
    assert packet is not None
    assert packet.clock == 1000 - 900
    assert packet.context == 200
    assert len(packet.samples) == 2
    assert packet.samples[0] == (10, 20, 30)
    assert packet.first_x == 10
    assert packet.first_y == 20
    assert packet.first_z == 30
    assert packet.motion_intensity > 0


def test_combined_log_row_schema():
    row = CombinedLogRow(
        timestamp="2026-08-18T11:00:00.000",
        elapsed_ms=1000,
        device_mac="AA:BB:CC:DD:EE:01",
        connection_state="connected",
        data_type="D306_EDA",
        EDA_Raw_Value=50000,
        Stress_Index=35,
    )
    csv_row = row.to_csv_row()
    header = CombinedLogRow.header()
    assert len(csv_row) == len(header)
    assert csv_row[header.index("EDA_Raw_Value")] == 50000
    assert csv_row[header.index("Stress_Index")] == 35


def test_stream_and_computed_log_row_schemas():
    stream = StreamLogRow(
        timestamp="2026-08-18T11:00:00.000",
        elapsed_ms=1000,
        device_mac="AA:BB:CC:DD:EE:01",
        connection_state="connected",
        data_type="D306_EDA",
    )
    assert len(stream.to_csv_row()) == len(StreamLogRow.header())

    computed = ComputedLogRow(
        timestamp="2026-08-18T11:00:00.000",
        elapsed_ms=1000,
        device_mac="AA:BB:CC:DD:EE:01",
        connection_state="connected",
        data_type="D306_EDA_COMPUTED",
        Skin_Resistance_kOhm="120.5000",
    )
    assert len(computed.to_csv_row()) == len(ComputedLogRow.header())
    idx = ComputedLogRow.header().index("Skin_Resistance_kOhm")
    assert computed.to_csv_row()[idx] == "120.5000"


def test_imu_log_row_schema():
    imu = ImuLogRow(
        timestamp="2026-08-18T11:00:00.000",
        elapsed_ms=100,
        clock=50,
        context=0,
        motion_intensity="12.3456",
        x=1,
        y=2,
        z=3,
    )
    assert len(imu.to_csv_row()) == len(ImuLogRow.header())
    assert imu.to_csv_row() == [
        "2026-08-18T11:00:00.000",
        100,
        50,
        0,
        "12.3456",
        1,
        2,
        3,
        "",
    ]

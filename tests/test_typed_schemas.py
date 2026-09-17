"""Tests for strongly typed schemas and CSV row dataclasses."""

import struct

import pytest

from nuanic_ring.io.schemas import (
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
    # Fixed 92-byte hardware payload: 8-byte header (<II) + 14 * 6-byte samples (<hhh)
    samples = [(i * 10, i * 20, i * 30) for i in range(14)]
    sample_bytes = b"".join(struct.pack("<hhh", x, y, z) for x, y, z in samples)
    raw = struct.pack("<II", 100, 200) + sample_bytes
    assert len(raw) == 92

    packet = ImuBatchPacket.from_bytes(raw)
    assert packet is not None
    assert packet.clock == 100
    assert packet.context == 200
    assert len(packet.samples) == 14
    assert packet.samples[0] == (0, 0, 0)
    assert packet.samples[1] == (10, 20, 30)
    assert packet.first_x == 0
    assert packet.first_y == 0
    assert packet.first_z == 0
    assert packet.motion_intensity > 0

    # Strict length validation: non-92-byte payloads must be rejected
    assert ImuBatchPacket.from_bytes(b"\x00" * 20) is None
    assert ImuBatchPacket.from_bytes(b"\x00" * 91) is None
    assert ImuBatchPacket.from_bytes(b"\x00" * 93) is None
    assert ImuBatchPacket.from_bytes(b"") is None


def test_combined_log_row_schema():
    expected_header = [
        "timestamp",
        "elapsed_ms",
        "device_mac",
        "connection_state",
        "data_type",
        "EDA_Raw_Value",
        "Stress_Index",
        "D306_Clock",
        "D306_Context",
        "State_Code",
        "payload_hex",
        "full_packet_hex",
        "decoded_fields",
        "D306_Observed_Hz",
        "IMU_Observed_Hz",
        "Rate_Target_Hz",
        "Rate_Control_Status",
        "Equalize_Mode",
        "Equalize_WouldDrop",
    ]
    header = CombinedLogRow.header()
    assert header == expected_header

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
    assert len(csv_row) == 19
    assert csv_row[0] == "2026-08-18T11:00:00.000"
    assert csv_row[1] == 1000
    assert csv_row[2] == "AA:BB:CC:DD:EE:01"
    assert csv_row[3] == "connected"
    assert csv_row[4] == "D306_EDA"
    assert csv_row[5] == 50000
    assert csv_row[6] == 35
    assert csv_row[7] == ""  # D306_Clock default
    assert csv_row[16] == ""  # Rate_Control_Status default


def test_stream_and_computed_log_row_schemas():
    stream_expected_header = [
        "timestamp",
        "elapsed_ms",
        "device_mac",
        "connection_state",
        "data_type",
        "D306_Clock",
        "D306_Context",
        "EDA_Raw_Value",
        "Stress_Index",
        "State_Code",
        "payload_hex",
        "full_packet_hex",
        "decoded_fields",
        "marker_label",
        "marker_source",
    ]
    assert StreamLogRow.header() == stream_expected_header

    stream = StreamLogRow(
        timestamp="2026-08-18T11:00:00.000",
        elapsed_ms=1000,
        device_mac="AA:BB:CC:DD:EE:01",
        connection_state="connected",
        data_type="D306_EDA",
        EDA_Raw_Value=50000,
        Stress_Index=35,
    )
    stream_row = stream.to_csv_row()
    assert len(stream_row) == 15
    assert stream_row[0] == "2026-08-18T11:00:00.000"
    assert stream_row[7] == 50000
    assert stream_row[8] == 35

    computed_expected_header = [
        "timestamp",
        "elapsed_ms",
        "device_mac",
        "connection_state",
        "data_type",
        "Source_D306_Clock",
        "Source_D306_Context",
        "Skin_Resistance_kOhm",
        "Skin_Conductance_uS",
        "MM_Filtered_uS",
        "SCR_Frequency_Per_Min",
        "SCR_Amplitude",
        "MM_Arousal_Score",
        "MM_Calibrated",
        "D306_Observed_Hz",
        "IMU_Observed_Hz",
        "Rate_Target_Hz",
        "Rate_Control_Status",
        "Equalize_Mode",
        "Equalize_WouldDrop",
        "marker_label",
        "marker_source",
    ]
    assert ComputedLogRow.header() == computed_expected_header

    computed = ComputedLogRow(
        timestamp="2026-08-18T11:00:00.000",
        elapsed_ms=1000,
        device_mac="AA:BB:CC:DD:EE:01",
        connection_state="connected",
        data_type="D306_EDA_COMPUTED",
        Skin_Resistance_kOhm="120.5000",
    )
    computed_row = computed.to_csv_row()
    assert len(computed_row) == 22
    assert computed_row[7] == "120.5000"


def test_imu_log_row_schema():
    assert ImuLogRow.header() == [
        "timestamp",
        "elapsed_ms",
        "clock",
        "context",
        "motion_intensity",
        "x",
        "y",
        "z",
        "marker",
    ]

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
    assert len(imu.to_csv_row()) == 9
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

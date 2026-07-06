import struct
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.monitor import NuanicMonitor


def test_split_csv_rows_keep_streamed_and_computed_shapes():
    monitor = NuanicMonitor(enable_logging=False, csv_layout="split")
    monitor.capture_armed = True
    monitor.running = True
    monitor.start_time = datetime.now()

    stream_rows = []
    computed_rows = []
    combined_rows = []
    imu_rows = []

    monitor._enqueue_stream_log = lambda _state, row: stream_rows.append(row)
    monitor._enqueue_computed_log = lambda _state, row: computed_rows.append(row)
    monitor._enqueue_log = lambda _state, row: combined_rows.append(row)
    monitor._enqueue_imu_log = lambda _state, row: imu_rows.append(row)

    mac = "AA:BB:CC:DD:EE:FF"
    monitor._ensure_device_state(mac)

    d306_packet = struct.pack("<Qii", 1, 16_000_000, 42)
    monitor._make_stress_callback(mac)(None, d306_packet)

    imu_packet = struct.pack("<II", 3, 4) + b"".join(
        struct.pack("<hhh", idx, idx + 1, idx + 2) for idx in range(14)
    )
    monitor._make_imu_callback(mac)(None, imu_packet)

    monitor._make_raw_eda_callback(mac)(None, bytes([2]))
    monitor._make_live_eda_callback(mac)(None, bytes([1, 2, 3, 4]))
    monitor.add_marker("stimulus", source="test")

    assert stream_rows
    assert computed_rows
    assert combined_rows
    assert imu_rows
    assert len(imu_rows) == 15  # 14 data + 1 marker
    imu_data_rows = [r for r in imu_rows if r[8] == ""]
    assert len(imu_data_rows) == 14
    assert all(len(row) == 15 for row in stream_rows)
    assert all(len(row) == 22 for row in computed_rows)
    assert all(len(row) == 19 for row in combined_rows)
    assert all(len(row) == 9 for row in imu_rows)
    # All 14 unrolled rows share the same timestamp
    imu_timestamps = {row[0] for row in imu_data_rows}
    assert len(imu_timestamps) == 1
    # No IMU rows leak into the combined CSV
    assert not any(row[4] == "IMU_BATCH_468F" for row in combined_rows)
    # Marker propagates to IMU CSV
    imu_marker = next(row for row in imu_rows if row[8] != "")
    assert "stimulus" in imu_marker[8]

    d306_stream = next(row for row in stream_rows if row[4] == "D306_EDA")
    assert d306_stream[5:9] == [1, 16_000_000, 16_000_000, 42]

    d306_computed = next(row for row in computed_rows if row[4] == "D306_EDA_COMPUTED")
    assert d306_computed[7] == "16000.0000"
    assert d306_computed[8] == "0.0625"


def test_live_eda_raw_csv_layouts():
    # Test split layout with 14-byte Raw EDA packet
    monitor = NuanicMonitor(enable_logging=False, csv_layout="split")
    monitor.capture_armed = True
    monitor.running = True
    monitor.start_time = datetime.now()

    stream_rows = []
    computed_rows = []
    monitor._enqueue_stream_log = lambda _state, row: stream_rows.append(row)
    monitor._enqueue_computed_log = lambda _state, row: computed_rows.append(row)

    mac = "AA:BB:CC:DD:EE:FF"
    monitor._ensure_device_state(mac)

    import time

    # 14-byte packet: boot_count=10, timestamp_ms=123456, eda_ohm=50000 (50 kOhm -> 20 uS)
    raw_packet = struct.pack("<HQI", 10, 123456, 50000)
    monitor._make_live_eda_callback(mac)(None, raw_packet)

    state = monitor.device_states[mac]
    assert state.heartbeat_tick is True
    assert state.d306_observed_hz == 0.0

    # Second callback invocation after a small sleep to verify observed HZ calculation
    time.sleep(0.05)
    monitor._make_live_eda_callback(mac)(None, raw_packet)

    assert state.heartbeat_tick is False  # Toggled twice
    assert state.d306_observed_hz > 0.0

    assert len(stream_rows) == 2
    assert len(computed_rows) == 2
    assert stream_rows[0][4] == "LIVE_EDA_42DC"
    assert stream_rows[0][7] == "50000"  # EDA_Raw_Value
    assert computed_rows[0][4] == "LIVE_EDA_COMPUTED"
    assert computed_rows[0][7] == "50.0000"  # Skin_Resistance_kOhm
    assert computed_rows[0][8] == "20.0000"  # Skin_Conductance_uS

    # Test nuanic layout with 14-byte Raw EDA packet
    monitor_nuanic = NuanicMonitor(enable_logging=False, csv_layout="nuanic")
    monitor_nuanic.capture_armed = True
    monitor_nuanic.running = True
    monitor_nuanic.start_time = datetime.now()

    nuanic_rows = []
    monitor_nuanic._enqueue_log = lambda _state, row: nuanic_rows.append(row)
    monitor_nuanic._ensure_device_state(mac)
    monitor_nuanic._make_live_eda_callback(mac)(None, raw_packet)

    assert len(nuanic_rows) == 1
    # nuanic row layout: [mac, ts_unix, ts_str, dne, srl, srrn, eda] (7 columns)
    assert len(nuanic_rows[0]) == 7
    assert nuanic_rows[0][0] == mac
    assert nuanic_rows[0][3] == 0  # dne
    assert nuanic_rows[0][4] == 50000  # srl_ohms
    assert nuanic_rows[0][6] == 50000  # eda_val

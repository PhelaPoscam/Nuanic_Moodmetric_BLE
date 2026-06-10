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

    monitor._enqueue_stream_log = lambda _state, row: stream_rows.append(row)
    monitor._enqueue_computed_log = lambda _state, row: computed_rows.append(row)
    monitor._enqueue_log = lambda _state, row: combined_rows.append(row)

    mac = "AA:BB:CC:DD:EE:FF"
    monitor._ensure_device_state(mac)

    d306_packet = struct.pack("<IIII", 1, 2, 16_000_000, 42)
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
    assert all(len(row) == 22 for row in stream_rows)
    assert all(len(row) == 25 for row in computed_rows)
    assert all(len(row) == 30 for row in combined_rows)

    d306_stream = next(row for row in stream_rows if row[4] == "D306_EDA")
    assert d306_stream[5:9] == [1, 2, 16_000_000, 42]

    d306_computed = next(row for row in computed_rows if row[4] == "D306_EDA_COMPUTED")
    assert d306_computed[9] == "16000.0000"
    assert d306_computed[10] == "0.0625"

"""Verify eager CSV log-file initialization (no blocking I/O on the callback hot path)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.monitor import NuanicMonitor


def test_log_files_created_eagerly_on_state_creation(tmp_path):
    """Log files are created as soon as device state is created (in a running
    session), so the BLE notify callback never performs filesystem I/O.
    Writer tasks require a running event loop (callbacks run inside one), so
    they are only asserted in the async test below."""
    monitor = NuanicMonitor(
        enable_logging=True, csv_layout="both", log_dir=str(tmp_path)
    )
    monitor.running = True
    mac = "AA:BB:CC:DD:EE:FF"

    state = monitor._ensure_device_state(mac)

    # Files are created eagerly (before any packet arrives)
    assert state.log_file is not None
    assert state.stream_log_file is not None
    assert state.computed_log_file is not None
    assert state.imu_log_file is not None
    assert state.log_file.exists()
    assert state.stream_log_file.exists()
    assert state.computed_log_file.exists()
    assert state.imu_log_file.exists()


def test_writer_tasks_created_inside_event_loop(tmp_path):
    """Inside a running loop, writer tasks are created eagerly per queue."""
    monitor = NuanicMonitor(
        enable_logging=True, csv_layout="both", log_dir=str(tmp_path)
    )
    monitor.running = True
    mac = "AA:BB:CC:DD:EE:FF"

    async def create_state():
        return monitor._ensure_device_state(mac)

    state = asyncio.run(create_state())

    assert state.writer_task is not None
    assert state.stream_writer_task is not None
    assert state.computed_writer_task is not None
    assert state.imu_writer_task is not None


def test_writer_loop_flushes_rows_to_disk(tmp_path):
    """A row enqueued through the callback path is written to the CSV by the writer task."""
    monitor = NuanicMonitor(
        enable_logging=True, csv_layout="combined", log_dir=str(tmp_path)
    )
    monitor.running = True
    mac = "AA:BB:CC:DD:EE:FF"

    async def exercise():
        monitor._ensure_device_state(mac)
        monitor.add_marker("stimulus", source="test")
        await asyncio.sleep(
            0.5
        )  # let the writer task drain the queue (0.2s timeout + write)

    asyncio.run(exercise())

    state = monitor.device_states[mac]
    content = state.log_file.read_text(encoding="utf-8")
    # Header + at least one marker row
    assert "timestamp" in content
    assert "stimulus" in content

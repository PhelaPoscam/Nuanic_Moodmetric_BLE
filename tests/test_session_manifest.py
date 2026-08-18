"""Tests for session manifest generation and SHA-256 provenance."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nuanic_ring.manifest import (
    DeviceSessionStats,
    SessionConfiguration,
    SessionManifest,
    compute_sha256,
    generate_session_manifest,
)
from nuanic_ring.monitor import RingDeviceState


def test_compute_sha256(tmp_path: Path):
    test_file = tmp_path / "test.csv"
    test_file.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

    digest = compute_sha256(test_file)
    assert digest.startswith("sha256:")
    assert len(digest) == 7 + 64


def test_generate_session_manifest_creates_valid_json(tmp_path: Path):
    session_dir = tmp_path / "SessionDate_18-08-2026_11-00-00"
    csv_dir = session_dir / "csvs"
    csv_dir.mkdir(parents=True)

    stream_csv = csv_dir / "ring-EE01_streamed.csv"
    stream_csv.write_text(
        "timestamp,d306_clock\n2026-08-18T11:00:00,100\n", encoding="utf-8"
    )

    state = RingDeviceState(
        mac="AA:BB:CC:DD:EE:01",
        battery=88,
        battery_start=92,
        battery_end=88,
        d306_count=100,
        imu_batch_count=50,
        live_eda_count=10,
        state_count=5,
        dropped_rows=2,
        d306_observed_hz=15.98,
    )

    start = datetime(2026, 8, 18, 11, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 18, 11, 30, 0, tzinfo=timezone.utc)

    config = {
        "target_hz": 16.0,
        "operational_mode": "0x01 (raw_eda)",
        "filter_enabled": False,
        "csv_layout": "split",
    }

    manifest = generate_session_manifest(
        session_dir=session_dir,
        session_id="SessionDate_18-08-2026_11-00-00",
        start_time=start,
        end_time=end,
        configuration=config,
        device_states={"AA:BB:CC:DD:EE:01": state},
        sdk_version="0.2.0",
    )

    manifest_file = session_dir / "session_manifest.json"
    assert manifest_file.exists()

    with open(manifest_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["sdk_version"] == "0.2.0"
    assert data["session_id"] == "SessionDate_18-08-2026_11-00-00"
    assert data["duration_seconds"] == 1800.0
    assert data["configuration"]["target_hz"] == 16.0
    assert "AA:BB:CC:DD:EE:01" in data["devices"]

    dev_data = data["devices"]["AA:BB:CC:DD:EE:01"]
    assert dev_data["battery_start"] == 92
    assert dev_data["battery_end"] == 88
    assert dev_data["total_packets_received"] == 165
    assert dev_data["dropped_rows"] == 2
    assert dev_data["mean_observed_hz"] == 15.98

    # Verify checksums include relative path
    assert "csvs/ring-EE01_streamed.csv" in data["checksums"]
    expected_hash = compute_sha256(stream_csv)
    assert data["checksums"]["csvs/ring-EE01_streamed.csv"] == expected_hash

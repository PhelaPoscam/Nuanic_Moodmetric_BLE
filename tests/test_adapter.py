"""Tests for the single-ring NuanicAdapter: decoding and the reconnect watchdog."""

import asyncio
import math
import statistics
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.core.adapter import NuanicAdapter, parse_eda_packet
from nuanic_ring.io.schemas import ImuBatchPacket

MAC = "AA:BB:CC:DD:EE:FF"


def _fake_connector(client):
    return SimpleNamespace(
        scanner=SimpleNamespace(
            find_device=AsyncMock(return_value=SimpleNamespace(address=MAC.lower())),
            list_available_rings_with_paired=AsyncMock(return_value=[]),
        ),
        connect_device=AsyncMock(return_value=True),
        set_mode=AsyncMock(return_value=True),
        get_client=MagicMock(return_value=client),
        subscribe_to_raw_eda_ohms=AsyncMock(return_value=True),
        subscribe_to_live_dne=AsyncMock(return_value=True),
        subscribe_to_imu_motion=AsyncMock(return_value=True),
        disconnect=AsyncMock(),
    )


def test_parse_eda_packet_gives_one_shape_for_both_modes():
    raw = parse_eda_packet(struct.pack("<HQI", 12, 1690000000123, 45000))
    assert (raw["clock"], raw["context"], raw["eda_value"]) == (
        12,
        1690000000123,
        45000,
    )
    assert raw["dne_stress_index"] == 0

    d306 = parse_eda_packet(struct.pack("<Qii", 1719999999000, 35000, 4))
    assert d306["eda_value"] == 35000
    assert d306["dne_stress_index"] == 4

    assert parse_eda_packet(b"short") is None


def test_imu_motion_intensity_is_stdev_of_magnitudes():
    samples = [(i * 10, i * 20, i * 30) for i in range(14)]
    raw = struct.pack("<II", 1, 2) + b"".join(struct.pack("<hhh", *s) for s in samples)
    expected = statistics.stdev(math.sqrt(x * x + y * y + z * z) for x, y, z in samples)
    assert ImuBatchPacket.from_bytes(raw).motion_intensity == pytest.approx(expected)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        NuanicAdapter(mode="turbo")


@pytest.mark.asyncio
async def test_connect_discovers_sets_mode_and_subscribes():
    client = SimpleNamespace(is_connected=True)
    adapter = NuanicAdapter(mode="live", enable_watchdog=False)
    adapter.connector = _fake_connector(client)

    assert await adapter.connect() is True
    assert adapter.connected_mac == MAC
    assert adapter.is_connected is True
    adapter.connector.set_mode.assert_awaited_once_with(0x02, address=MAC)
    adapter.connector.subscribe_to_live_dne.assert_awaited_once()
    adapter.connector.subscribe_to_raw_eda_ohms.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_reconnects_same_ring_after_link_loss():
    client = SimpleNamespace(is_connected=True)
    statuses = []
    adapter = NuanicAdapter(
        status_callback=statuses.append,
        watchdog_interval=0.01,
        freeze_timeout=5.0,
        reconnect_backoff=0.01,
    )
    adapter.connector = _fake_connector(client)
    assert await adapter.connect() is True
    client.is_connected = False

    async def _reconnect_side_effect(*_args, **_kwargs):
        client.is_connected = True
        return True

    adapter.connector.connect_device.side_effect = _reconnect_side_effect
    for _ in range(100):
        if "Connected (raw_eda)! Streaming..." in statuses:
            break
        await asyncio.sleep(0.01)
    await adapter.disconnect()

    assert statuses[0] == "Watchdog: no link; reconnecting..."
    assert "Connected (raw_eda)! Streaming..." in statuses
    # Reconnect targets the known ring instead of rescanning for any ring.
    adapter.connector.scanner.find_device.assert_awaited_once()
    assert adapter.connector.connect_device.await_args.args[0] == MAC
    assert adapter.connector.subscribe_to_imu_motion.await_count == 2


@pytest.mark.asyncio
async def test_watchdog_reconnects_on_packet_freeze_and_survives_bad_status_callback():
    client = SimpleNamespace(is_connected=True)

    def bad_status(_msg):
        raise RuntimeError("ui broke")

    adapter = NuanicAdapter(
        target_address=MAC,
        status_callback=bad_status,
        watchdog_interval=0.01,
        freeze_timeout=0.02,
        reconnect_backoff=0.01,
    )
    adapter.connector = _fake_connector(client)

    assert await adapter.connect() is True
    for _ in range(200):
        if adapter.connector.connect_device.await_count >= 2:
            break
        await asyncio.sleep(0.02)
    await adapter.disconnect()

    assert adapter.connector.connect_device.await_count >= 2
    adapter.connector.disconnect.assert_any_await(address=MAC)

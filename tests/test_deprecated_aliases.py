"""Tests for canonical GATT methods and deprecation warnings."""

import warnings
from unittest.mock import AsyncMock, patch

import pytest

from nuanic_ring.connector import NuanicConnector


@pytest.mark.asyncio
async def test_canonical_and_deprecated_subscriptions():
    connector = NuanicConnector()
    connector._subscribe = AsyncMock(return_value=True)
    connector._unsubscribe = AsyncMock()

    cb = lambda char, data: None

    # Test Canonical methods
    assert await connector.subscribe_to_finger_state(cb, address="AA:BB:CC:DD:EE:01")
    connector._subscribe.assert_called_with(
        connector.STATE_UUID, cb, "AA:BB:CC:DD:EE:01", "finger state"
    )

    assert await connector.subscribe_to_live_dne(cb, address="AA:BB:CC:DD:EE:01")
    connector._subscribe.assert_called_with(
        connector.LIVE_DNE_UUID, cb, "AA:BB:CC:DD:EE:01", "live DNE stress data"
    )

    assert await connector.subscribe_to_raw_eda_ohms(cb, address="AA:BB:CC:DD:EE:01")
    connector._subscribe.assert_called_with(
        connector.LIVE_EDA_UUID, cb, "AA:BB:CC:DD:EE:01", "raw EDA ohms notifications"
    )

    assert await connector.subscribe_to_imu_motion(cb, address="AA:BB:CC:DD:EE:01")
    connector._subscribe.assert_called_with(
        connector.IMU_BATCH_UUID, cb, "AA:BB:CC:DD:EE:01", "IMU motion data"
    )

    # Test Deprecated methods trigger DeprecationWarning
    with pytest.deprecated_call():
        assert await connector.subscribe_to_stress(cb, address="AA:BB:CC:DD:EE:01")

    with pytest.deprecated_call():
        assert await connector.subscribe_to_imu(cb, address="AA:BB:CC:DD:EE:01")

    with pytest.deprecated_call():
        assert await connector.subscribe_to_raw_eda(cb, address="AA:BB:CC:DD:EE:01")

    with pytest.deprecated_call():
        assert await connector.subscribe_to_live_eda(cb, address="AA:BB:CC:DD:EE:01")

    # Test unsubscriptions
    await connector.unsubscribe_from_finger_state(address="AA:BB:CC:DD:EE:01")
    connector._unsubscribe.assert_called_with(connector.STATE_UUID, "AA:BB:CC:DD:EE:01")

    with pytest.deprecated_call():
        await connector.unsubscribe_from_stress(address="AA:BB:CC:DD:EE:01")
    connector._unsubscribe.assert_called_with(
        connector.LIVE_DNE_UUID, "AA:BB:CC:DD:EE:01"
    )

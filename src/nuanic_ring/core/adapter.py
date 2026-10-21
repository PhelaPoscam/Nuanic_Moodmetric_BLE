"""Single-ring adapter: connect, decode, and keep one Nuanic ring streaming."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from nuanic_ring.core.connector import NuanicConnector
from nuanic_ring.telemetry.callbacks import (
    parse_468f_imu_batch,
    parse_d306_packet,
    parse_live_eda_packet,
)

_log = logging.getLogger(__name__)

MODES = {label: mode for mode, label in NuanicConnector.MODE_LABELS.items()}

PacketCallback = Callable[[Dict[str, Any]], None]


def parse_eda_packet(data: bytes) -> Optional[Dict[str, Any]]:
    """Decode either EDA notification (14 B raw or 16 B D306) into one dict shape.

    Raw EDA gains the D306 compatibility keys so callers can log both modes
    through the same columns; it has no onboard DNE, so that index is 0.
    """
    if len(data) != 14:
        return parse_d306_packet(data)
    parsed = parse_live_eda_packet(data)
    if parsed is None:
        return None
    return {
        **parsed,
        "clock": parsed["boot_count"],
        "context": parsed["timestamp_ms"],
        "eda_value": parsed["eda_ohm"],
        "dne_stress_index": 0,
    }


class NuanicAdapter:
    """One ring with a link watchdog: reconnects on link loss or packet freeze.

    ``connect()`` returns whether the first attempt succeeded; the watchdog keeps
    retrying either way, so a ring switched on late is still picked up. EDA can
    go silent for ~60 s after a mode change (calibration), but IMU batches keep
    flowing, so the freeze check counts packets from both streams.
    """

    def __init__(
        self,
        target_address: Optional[str] = None,
        mode: str = "raw_eda",
        eda_callback: Optional[PacketCallback] = None,
        imu_callback: Optional[PacketCallback] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        timeout: float = 20.0,
        enable_watchdog: bool = True,
        watchdog_interval: float = 3.0,
        freeze_timeout: float = 10.0,
        reconnect_backoff: float = 2.0,
        max_backoff: float = 30.0,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {sorted(MODES)}, got {mode!r}")
        self.target_address = target_address
        self.mode = mode
        self.eda_callback = eda_callback
        self.imu_callback = imu_callback
        self.status_callback = status_callback
        self.enable_watchdog = enable_watchdog
        self.watchdog_interval = watchdog_interval
        self.freeze_timeout = freeze_timeout
        self.reconnect_backoff = reconnect_backoff
        self.max_backoff = max_backoff

        self.connector = NuanicConnector(
            timeout=timeout,
            target_address=target_address,
            pair_on_connect=True,
        )
        self.connected_mac: str = "-"
        self.last_packet_time = 0.0
        self._attempt = 0
        self._running = False
        self._watchdog_task: Optional[asyncio.Task[None]] = None

    @property
    def is_connected(self) -> bool:
        if self.connected_mac == "-":
            return False
        client = self.connector.get_client(self.connected_mac)
        return bool(client and getattr(client, "is_connected", False))

    async def connect(self) -> bool:
        """Connect once and start the watchdog; True if the ring is streaming."""
        self._running = True
        ok = await self._connect()
        if self.enable_watchdog and (
            self._watchdog_task is None or self._watchdog_task.done()
        ):
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        return ok

    async def _resolve(self) -> Tuple[Optional[str], Any]:
        """Address (and BLEDevice, if scanned) of the ring to connect to."""
        # Once connected, stick to that ring: a broad rescan could grab another one.
        known = self.target_address or (
            self.connected_mac if self.connected_mac != "-" else None
        )
        if known:
            return known.upper(), None
        scanner = self.connector.scanner
        device = await scanner.find_device()
        if device:
            return device.address.upper(), device
        rings = await scanner.list_available_rings_with_paired(
            stop_if_found=True, silent=True
        )
        if rings:
            return rings[0]["address"].upper(), rings[0].get("device")
        return None, None

    async def _connect(self) -> bool:
        self.last_packet_time = 0.0  # unhealthy until fully subscribed
        address, device = await self._resolve()
        if not address:
            _log.warning("No Nuanic ring discovered during BLE scan.")
            return False
        if not await self.connector.connect_device(address, device):
            _log.error("Failed to connect to Nuanic ring %s", address)
            return False
        self.connected_mac = address

        await self.connector.set_mode(MODES[self.mode], address=address)
        if self.mode == "raw_eda":
            eda_ok = await self.connector.subscribe_to_raw_eda_ohms(
                self._on_eda, address=address
            )
        else:
            eda_ok = await self.connector.subscribe_to_live_dne(
                self._on_eda, address=address
            )
        imu_ok = await self.connector.subscribe_to_imu_motion(
            self._on_imu, address=address
        )
        if not (eda_ok and imu_ok):
            _log.error("Nuanic ring %s: stream subscription failed", address)
            return False

        self.last_packet_time = time.monotonic()
        _log.info("Nuanic ring %s connected in mode '%s'", address, self.mode)
        return True

    def _on_eda(self, _sender: Any, data: bytearray) -> None:
        self.last_packet_time = time.monotonic()
        parsed = parse_eda_packet(bytes(data))
        if parsed and self.eda_callback:
            self.eda_callback(parsed)

    def _on_imu(self, _sender: Any, data: bytearray) -> None:
        self.last_packet_time = time.monotonic()
        parsed = parse_468f_imu_batch(bytes(data))
        if parsed and self.imu_callback:
            self.imu_callback(parsed)

    async def _watchdog_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.watchdog_interval)
            healthy = (
                self.is_connected
                and time.monotonic() - self.last_packet_time <= self.freeze_timeout
            )
            if healthy:
                self._attempt = 0
            else:
                await self._reconnect()

    async def _reconnect(self) -> None:
        """Tear down, back off (2, 4, 8 ... max_backoff s), and connect again."""
        self._attempt += 1
        reason = "stream frozen" if self.is_connected else "no link"
        self._notify(f"Watchdog: {reason}; reconnecting...")
        if self.connected_mac != "-":
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self.connector.disconnect(address=self.connected_mac), timeout=5.0
                )
        delay = self.reconnect_backoff * 2 ** (min(self._attempt, 6) - 1)
        await asyncio.sleep(min(self.max_backoff, delay))
        try:
            if await self._connect():
                self._notify(f"Connected ({self.mode})! Streaming...")
        except Exception as exc:
            _log.error("Nuanic reconnect failed: %s", exc)
            self._notify(f"Reconnect failed ({exc})")

    def _notify(self, msg: str) -> None:
        _log.info("Nuanic %s: %s", self.connected_mac, msg)
        if self.status_callback:
            # A raising UI callback must not abort reconnects or the watchdog.
            try:
                self.status_callback(msg)
            except Exception:
                _log.exception("Nuanic status callback failed")

    async def disconnect(self) -> None:
        """Stop the watchdog and disconnect the ring."""
        self._running = False
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
        self._watchdog_task = None
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.connector.disconnect(), timeout=5.0)
        self.connected_mac = "-"

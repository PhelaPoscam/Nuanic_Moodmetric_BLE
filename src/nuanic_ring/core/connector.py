"""BLE connection and device management for Nuanic ring(s)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import platform
import struct
import subprocess
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from bleak import BleakClient, BleakGATTCharacteristic

from nuanic_ring.core.profiles import (
    BATTERY_UUID,
    COMMAND_UUID,
    IMU_BATCH_UUID,
    LIVE_DNE_UUID,
    LIVE_EDA_UUID,
    NUANIC_SERVICE_UUID,
    REALTIME_UUID,
    SAMPLE_RATE_UUID,
    STATE_UUID,
    STORAGE_FORMAT_UUID,
    STORAGE_REWIND_UUID,
    STORAGE_USAGE_UUID,
    STORAGE_UUID,
)
from nuanic_ring.core.scanner import (
    RingScanner,
    _load_last_address,
    _reset_bluetooth_radio,
    _save_last_address,
)

_log = logging.getLogger(__name__)


class NuanicConnector:
    """Handles BLE connections to one or many Nuanic/Moodmetric rings."""

    # ── Operational modes (write to STORAGE_FORMAT / CONFIG_3) ──
    MODE_STANDBY = 0x00  # Standby / None (Physiology OFF, IMU + finger-detect active)
    MODE_RAW_EDA = 0x01  # Format 1: Unprocessed Raw EDA on 42dcb71b (<HQI, 14-byte)
    MODE_LIVE = (
        0x02  # Format 2: Preprocessed Instant EDA + DNE on d306262b (<Qii, 16-byte)
    )
    MODE_RESEARCH = 0x03  # Undocumented/legacy variant of Format 2 (Preprocessed + DNE)

    MODE_LABELS = {
        0x00: "standby",
        0x01: "raw_eda",
        0x02: "live",
        0x03: "research",
    }

    # GATT UUIDs (Empirically verified & decoded)
    NUANIC_SERVICE_UUID = NUANIC_SERVICE_UUID
    STATE_UUID = STATE_UUID
    SAMPLE_RATE_UUID = SAMPLE_RATE_UUID
    REALTIME_UUID = REALTIME_UUID
    LIVE_DNE_UUID = LIVE_DNE_UUID
    LIVE_EDA_UUID = LIVE_EDA_UUID
    STORAGE_UUID = STORAGE_UUID
    STORAGE_FORMAT_UUID = STORAGE_FORMAT_UUID
    STORAGE_REWIND_UUID = STORAGE_REWIND_UUID
    STORAGE_USAGE_UUID = STORAGE_USAGE_UUID
    COMMAND_UUID = COMMAND_UUID
    IMU_BATCH_UUID = IMU_BATCH_UUID
    BATTERY_UUID = BATTERY_UUID

    def __init__(
        self,
        timeout: float = 7.0,
        max_scan_attempts: int = 3,
        max_connect_attempts: int = 3,
        connect_backoff_seconds: float = 2.0,
        target_address: Optional[str] = None,
        unpair_on_disconnect: bool = False,
        pair_on_connect: bool = True,
        auto_sync_time: bool = True,
    ) -> None:
        self.max_connect_attempts = max_connect_attempts
        self.connect_backoff_seconds = connect_backoff_seconds
        self.target_address: Optional[str] = target_address
        self.unpair_on_disconnect = unpair_on_disconnect
        self.pair_on_connect = pair_on_connect
        self.auto_sync_time = auto_sync_time
        self.client: Optional[BleakClient] = None
        self.device: Optional[Any] = None
        self._disconnect_event = asyncio.Event()

        # Multi-device runtime registries keyed by BLE MAC address.
        self.clients: Dict[str, BleakClient] = {}
        self.devices: Dict[str, Any] = {}
        self._disconnect_events: Dict[str, asyncio.Event] = {}
        self._disconnect_events_lock = asyncio.Lock()

        # Delegate scanning to a dedicated scanner.
        self._scanner = RingScanner(
            timeout=timeout,
            max_scan_attempts=max_scan_attempts,
            target_address=target_address,
            pair_on_connect=pair_on_connect,
        )

    # ------------------------------------------------------------------
    # Delegated scanning methods
    # ------------------------------------------------------------------

    async def _reset_bluetooth_radio(self) -> bool:
        """Delegate to module-level radio reset."""
        return await _reset_bluetooth_radio()

    async def find_device(self):
        """Scan for Nuanic ring (delegated to ``RingScanner``)."""
        device = await self._scanner.find_device()
        self.device = device
        return device

    async def list_available_rings(
        self,
        include_device: bool = False,
        scan_timeout: float = 6.0,
        attempts: int = 3,
        retry_delay: float = 1.0,
        stop_if_found: bool = True,
        silent: bool = False,
    ):
        """Scan and return list of all available Nuanic rings."""
        return await self._scanner.list_available_rings(
            include_device=include_device,
            scan_timeout=scan_timeout,
            attempts=attempts,
            retry_delay=retry_delay,
            stop_if_found=stop_if_found,
            silent=silent,
        )

    async def list_available_rings_with_paired(
        self,
        scan_timeout: float = 6.0,
        attempts: int = 3,
        stop_if_found: bool = True,
        silent: bool = False,
    ):
        """Return discoverable rings plus Windows paired rings (if any)."""
        return await self._scanner.list_available_rings_with_paired(
            scan_timeout=scan_timeout,
            attempts=attempts,
            stop_if_found=stop_if_found,
            silent=silent,
        )

    async def discover_all_matching_rings(
        self,
        include_device: bool = True,
        scan_timeout: float = 6.0,
        attempts: int = 3,
        retry_delay: float = 0.5,
        stop_if_found: bool = True,
        silent: bool = False,
    ) -> List[Dict[str, Any]]:
        """Discover all visible Nuanic/Moodmetric rings."""
        return await self._scanner.discover_all_matching_rings(
            include_device=include_device,
            scan_timeout=scan_timeout,
            attempts=attempts,
            retry_delay=retry_delay,
            stop_if_found=stop_if_found,
            silent=silent,
        )

    async def select_ring_interactive(self):
        """Interactive ring selection menu."""
        print("\n" + "=" * 60)
        print("RING SELECTION")
        print("=" * 60)

        rings = await self._scanner.list_available_rings_with_paired()

        if not rings:
            print("[!] No Nuanic rings found.")
            print(
                "[BT-RESET] Stale connection detected - resetting Bluetooth adapter..."
            )
            reset_ok = await _reset_bluetooth_radio()
            if reset_ok:
                print("[BT-RESET] Rescanning after radio reset...")
                rings = await self._scanner.list_available_rings_with_paired()

            if not rings:
                cached = _load_last_address()
                if cached:
                    print(
                        f"[HINT] Ring still not visible - trying direct "
                        f"reconnect to {cached}"
                    )
                    print("[HINT] If this also fails, turn the ring off/on.")
                    self.target_address = cached
                    return cached
                print("[!] No ring address cached. Turn the ring off/on.")
                return None

        print(f"\nFound {len(rings)} ring(s):\n")

        for idx, ring in enumerate(rings, 1):
            src = ring.get("source", "scan")
            src_tag = "SCAN" if src == "scan" else "PAIRED"
            print(f"  [{idx}] {ring['name']:15} | MAC: {ring['address']} | {src_tag}")

        if len(rings) == 1:
            print(f"\nAuto-selecting: {rings[0]['name']} ({rings[0]['address']})")
            self.target_address = rings[0]["address"]
            self.device = rings[0].get("device")
            print("=" * 60 + "\n")
            return rings[0]["address"]

        while True:
            try:
                loop = asyncio.get_event_loop()
                choice = await loop.run_in_executor(
                    None, input, f"\nSelect ring (1-{len(rings)}) or 'q' to cancel: "
                )
                choice = choice.strip()

                if choice.lower() == "q":
                    print("Cancelled.\n")
                    return None

                choice_idx = int(choice) - 1
                if 0 <= choice_idx < len(rings):
                    selected = rings[choice_idx]
                    self.target_address = selected["address"]
                    self.device = selected.get("device")
                    print(f"\nSelected: {selected['name']} ({selected['address']})")
                    print("=" * 60 + "\n")
                    return selected["address"]
                else:
                    print(f"Invalid choice. Enter 1-{len(rings)}")
            except ValueError:
                print(f"Invalid input. Enter 1-{len(rings)} or 'q'")

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _on_disconnect_for(self, address: str):
        """Factory for per-device disconnected callbacks."""

        def _cb(_client):
            event = self._disconnect_events.get(address)
            if event:
                event.set()
            if (
                self.client
                and getattr(self.client, "address", "").lower() == address.lower()
            ):
                self._disconnect_event.set()
            print(f"[DISC] BLE disconnect callback fired for {address}")

        return _cb

    def _create_bleak_client(self, target, disconnected_callback=None):
        """Create BleakClient with robust Windows-friendly arguments."""
        kwargs = {
            "timeout": self._scanner.timeout,
            "disconnected_callback": disconnected_callback,
        }

        if platform.system() == "Windows":
            kwargs["use_cached_services"] = False

        try:
            params = inspect.signature(BleakClient).parameters
            if "pair" in params:
                kwargs["pair"] = self.pair_on_connect
        except Exception:
            pass

        return BleakClient(target, **kwargs)

    async def _cleanup_client(self, address: Optional[str] = None):
        """Strict cleanup of existing BLE client state to prevent zombie connections."""
        target_client = self.clients.get(address.upper()) if address else self.client
        if target_client is None:
            return

        try:
            if getattr(target_client, "is_connected", False):
                if not address:
                    self._disconnect_event.clear()
                else:
                    event = self._disconnect_events.get(address.upper())
                    if event:
                        event.clear()

                await self._stop_all_notifications(target_client)

                print(
                    f"[CLEANUP] Disconnecting BleakClient{' for ' + address if address else ''}..."
                )
                await target_client.disconnect()

                try:
                    if not address:
                        await asyncio.wait_for(
                            self._disconnect_event.wait(), timeout=5.0
                        )
                    else:
                        event = self._disconnect_events.get(address.upper())
                        if event:
                            await asyncio.wait_for(event.wait(), timeout=5.0)
                    print("[CLEANUP] OS confirmed disconnect.")
                except asyncio.TimeoutError:
                    print("[CLEANUP] Warning: OS disconnect callback timed out.")
        except Exception as e:
            print(f"[CLEANUP] Error during disconnect: {e}")
        finally:
            await self._teardown_client_backend(target_client, address)

    async def _stop_all_notifications(self, target_client: BleakClient) -> None:
        """Best-effort stop of all notification streams on a client."""
        await asyncio.gather(
            *(
                target_client.stop_notify(char_uuid)
                for char_uuid in [
                    self.LIVE_DNE_UUID,
                    self.IMU_BATCH_UUID,
                    self.STATE_UUID,
                    self.LIVE_EDA_UUID,
                ]
            ),
            return_exceptions=True,
        )

    async def _teardown_client_backend(
        self, target_client: BleakClient, address: Optional[str] = None
    ) -> None:
        """Release OS-level BLE backend resources and drop the client registry entry."""
        try:
            if hasattr(target_client, "_backend"):
                if (
                    hasattr(target_client._backend, "_session")
                    and target_client._backend._session
                ):
                    target_client._backend._session.close()
                if (
                    hasattr(target_client._backend, "_device")
                    and target_client._backend._device
                ):
                    target_client._backend._device.close()
        except Exception:
            pass

        try:
            target_client.set_disconnected_callback(None)  # type: ignore[attr-defined]
        except Exception:
            pass

        if not address:
            self.client = None
        else:
            self.clients.pop(address.upper(), None)

    async def connect(self):
        """Connect to Nuanic ring with automatic retry and recovery."""
        await self._cleanup_client()
        if not self.target_address:
            selected = await self.select_ring_interactive()
            if not selected:
                print("[FAIL] No ring selected\n")
                return False
        if not self.device:
            try:
                self.device = await self.find_device()
            except Exception:
                pass
        return await self.connect_device(self.target_address, device=self.device)

    async def _get_or_create_disconnect_event(self, address: str) -> asyncio.Event:
        """Thread-safe fetch-or-create for per-device disconnect event."""
        async with self._disconnect_events_lock:
            event = self._disconnect_events.get(address)
            if event is None:
                event = asyncio.Event()
                self._disconnect_events[address] = event
            event.clear()
            return event

    async def connect_device(self, address: str, device: Any = None) -> bool:
        """Connect one device and register it in the multi-device registry."""
        address = address.upper()
        event = await self._get_or_create_disconnect_event(address)

        if device is None and platform.system() == "Windows":
            try:
                old_target = self._scanner.target_address
                self._scanner.target_address = address
                device = await self._scanner.find_device()
                self._scanner.target_address = old_target
            except Exception:
                pass

        target = device or address
        client = self._create_bleak_client(
            target,
            disconnected_callback=self._on_disconnect_for(address),
        )

        for attempt in range(1, self.max_connect_attempts + 1):
            try:
                await client.connect()
                if not getattr(client, "is_connected", False):
                    raise RuntimeError("connection not established")

                if not self.pair_on_connect:
                    try:
                        await client.pair()
                    except Exception:
                        pass

                await self._register_connected_device(address, client, device)

                return True
            except asyncio.CancelledError:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise
            except Exception as e:
                _log.warning(
                    "[CONN-FAIL] %s attempt %d/%d: %s: %s",
                    address,
                    attempt,
                    self.max_connect_attempts,
                    type(e).__name__,
                    e,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if attempt < self.max_connect_attempts:
                    await asyncio.sleep(self.connect_backoff_seconds)

        return False

    async def _register_connected_device(
        self, address: str, client: BleakClient, device: Any = None
    ) -> None:
        """Register a connected client/device and perform automatic boot-time clock sync."""
        self.clients[address] = client

        if device is not None:
            self.devices[address] = device

        self.client = client
        self.device = device
        self.target_address = address
        _save_last_address(address)

        # Automatic boot time sync to resolve 1970 timestamp epoch
        if self.auto_sync_time:
            try:
                await self.sync_time(address=address)
            except Exception as sync_exc:
                _log.debug(
                    "Automatic clock sync on boot failed for %s: %s",
                    address,
                    sync_exc,
                )

    async def connect_multiple(
        self,
        addresses=None,
        max_devices=None,
        stagger_delay: float = 1.25,
        scan_timeout: float = 4.0,
        scan_attempts: int = 2,
    ) -> Dict[str, bool]:
        """Connect to many rings with staggered timing."""
        results = {}
        discovered = await self.discover_all_matching_rings(
            include_device=True,
            scan_timeout=scan_timeout,
            attempts=scan_attempts,
            retry_delay=0.5,
        )
        discovered_by_addr = {d["address"]: d for d in discovered}

        target_addresses = list(addresses or discovered_by_addr.keys())
        if max_devices is not None:
            target_addresses = target_addresses[: max(0, max_devices)]

        for idx, address in enumerate(target_addresses):
            entry = discovered_by_addr.get(address)
            ok = await self.connect_device(
                address=address, device=(entry or {}).get("device")
            )
            results[address] = ok
            if idx < len(target_addresses) - 1 and stagger_delay > 0:
                await asyncio.sleep(stagger_delay)

        return results

    async def _disconnect_one(self, address: str) -> None:
        """Clean up a single device's client and device registry entry."""
        address = address.upper()
        if address not in self.clients:
            return
        try:
            await self._cleanup_client(address)
        except Exception:
            pass
        finally:
            self.devices.pop(address, None)
        _log.info("[OK] Disconnected %s", address)

    async def _disconnect_all(self) -> None:
        """Clean up every registered client and the primary connection."""
        had_any = False
        for addr in list(self.clients.keys()):
            had_any = True
            try:
                await self._cleanup_client(addr)
            except Exception:
                pass
            finally:
                self.devices.pop(addr, None)

        if self.client:
            was_connected = bool(getattr(self.client, "is_connected", False))
            await self._cleanup_client()
            if was_connected:
                had_any = True

        if had_any:
            _log.info("[OK] Disconnected")
        else:
            _log.info("[INFO] No active BLE connection to close")

    async def disconnect(self, address: Optional[str] = None) -> None:
        """Disconnect from ring."""
        if address:
            await self._disconnect_one(address)
        else:
            await self._disconnect_all()

        if self.unpair_on_disconnect and self.device:
            await self._unpair_device()

    async def _unpair_device(self):
        """Remove device from Windows Bluetooth pairing."""
        if not self.device:
            return

        try:
            ble_address = self.device.address.replace(":", "")
            ps_cmd = (
                f"Remove-Item -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\BTHPORT\\Parameters\\Keys\\*\\{ble_address}' "
                "-Force -ErrorAction SilentlyContinue; "
                f"Get-PnpDevice -FriendlyName '*{self.device.name}*' | Remove-PnpDevice -Force -ErrorAction SilentlyContinue"
            )

            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            if completed.returncode == 0 or completed.returncode == 1:
                print(f"[OK] Removed {self.device.name} from Windows Bluetooth")
            else:
                err_msg = (
                    completed.stderr.strip() if completed.stderr else "Unknown error"
                )
                print(f"[WARN] Unpair: {err_msg}")

        except subprocess.TimeoutExpired:
            print("[WARN] Unpair timeout")
        except Exception as e:
            print(f"[WARN] Unpair error: {e}")

    # ------------------------------------------------------------------
    # Battery and service discovery
    # ------------------------------------------------------------------

    async def read_battery(self, address: Optional[str] = None) -> Optional[int]:
        """Read battery level"""
        client = self.clients.get(address.upper()) if address else self.client
        if not client:
            return None

        try:
            value = await client.read_gatt_char(self.BATTERY_UUID)
            return value[0]
        except Exception as e:
            print(f"[FAIL] Battery read error: {e}")
            return None

    async def discover_services(self):
        """Discover and print all services and characteristics."""
        if not self.client or not self.client.is_connected:
            print("[FAIL] Not connected to any device.")
            return

        print(f"\n[INFO] Discovering services for {self.device.name}...")
        for service in self.client.services:
            print(f"  [SERVICE] {service.uuid}: {service.description}")
            for char in service.characteristics:
                print(
                    f"    [CHAR] {char.uuid}: {char.description}, Properties: {char.properties}"
                )
        print("[INFO] Service discovery complete.\n")

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_client(self, address: Optional[str] = None) -> Optional[BleakClient]:
        """Return a connected client by address or the legacy single client."""
        if address:
            return self.clients.get(address.upper())
        return self.client

    def connected_addresses(self) -> List[str]:
        """Return currently tracked connected addresses."""
        addrs = []
        for address, client in self.clients.items():
            if getattr(client, "is_connected", False):
                addrs.append(address)
        return addrs

    def _require_client(self, address: Optional[str] = None) -> Optional[BleakClient]:
        """Return a connected client, or *None* if unavailable."""
        client = self.get_client(address)
        if client and getattr(client, "is_connected", False):
            return client
        return None

    # ------------------------------------------------------------------
    # Subscription helpers
    # ------------------------------------------------------------------

    async def _subscribe(
        self,
        char_uuid: str,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
        label: str = "data",
    ) -> bool:
        client = self.clients.get(address.upper()) if address else self.client
        if not client or not getattr(client, "is_connected", False):
            _log.warning(f"[FAIL] Subscription error for {label}: Not connected")
            return False
        try:
            await client.start_notify(char_uuid, callback)
            _log.info(f"[OK] Subscribed to {label}")
            return True
        except Exception as e:
            _log.error(f"[FAIL] Subscription error for {label}: {e}")
            return False

    async def _unsubscribe(self, char_uuid: str, address: Optional[str] = None) -> None:
        client = self.clients.get(address.upper()) if address else self.client
        if client:
            try:
                await client.stop_notify(char_uuid)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Canonical GATT Subscription Methods
    # ------------------------------------------------------------------

    async def subscribe_to_finger_state(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to on-finger contact status indicator (0=init, 1=off, 2=on, 3=docked)."""
        return await self._subscribe(self.STATE_UUID, callback, address, "finger state")

    async def unsubscribe_from_finger_state(
        self, address: Optional[str] = None
    ) -> None:
        """Unsubscribe from finger state indicator notifications."""
        await self._unsubscribe(self.STATE_UUID, address)

    async def subscribe_to_live_dne(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to Instant EDA & DNE stress index (d306262b..., 16-byte)."""
        return await self._subscribe(
            self.LIVE_DNE_UUID, callback, address, "live DNE stress data"
        )

    async def unsubscribe_from_live_dne(self, address: Optional[str] = None) -> None:
        """Unsubscribe from live DNE stress data notifications."""
        await self._unsubscribe(self.LIVE_DNE_UUID, address)

    async def subscribe_to_raw_eda_ohms(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to uncalibrated Raw EDA resistance in Ohms (42dcb71b..., 14-byte)."""
        return await self._subscribe(
            self.LIVE_EDA_UUID,
            callback,
            address,
            "raw EDA ohms notifications",
        )

    async def unsubscribe_from_raw_eda_ohms(
        self, address: Optional[str] = None
    ) -> None:
        """Unsubscribe from raw EDA ohms notifications."""
        await self._unsubscribe(self.LIVE_EDA_UUID, address)

    async def subscribe_to_imu_motion(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to 14-sample accelerometer IMU batch stream (468f2717...)."""
        return await self._subscribe(
            self.IMU_BATCH_UUID, callback, address, "IMU motion data"
        )

    async def unsubscribe_from_imu_motion(self, address: Optional[str] = None) -> None:
        """Unsubscribe from IMU motion data notifications."""
        await self._unsubscribe(self.IMU_BATCH_UUID, address)

    # ------------------------------------------------------------------
    # Legacy Deprecated Subscription Aliases (Backwards Compatibility)
    # ------------------------------------------------------------------

    async def subscribe_to_stress(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Deprecated legacy alias for ``subscribe_to_live_dne``."""
        import warnings

        warnings.warn(
            "subscribe_to_stress is deprecated; use subscribe_to_live_dne instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.subscribe_to_live_dne(callback, address=address)

    async def unsubscribe_from_stress(self, address: Optional[str] = None) -> None:
        """Deprecated legacy alias for ``unsubscribe_from_live_dne``."""
        import warnings

        warnings.warn(
            "unsubscribe_from_stress is deprecated; use unsubscribe_from_live_dne instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.unsubscribe_from_live_dne(address=address)

    async def subscribe_to_imu(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Deprecated legacy alias for ``subscribe_to_imu_motion``."""
        import warnings

        warnings.warn(
            "subscribe_to_imu is deprecated; use subscribe_to_imu_motion instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.subscribe_to_imu_motion(callback, address=address)

    async def unsubscribe_from_imu(self, address: Optional[str] = None) -> None:
        """Deprecated legacy alias for ``unsubscribe_from_imu_motion``."""
        import warnings

        warnings.warn(
            "unsubscribe_from_imu is deprecated; use unsubscribe_from_imu_motion instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.unsubscribe_from_imu_motion(address=address)

    async def subscribe_to_raw_eda(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Deprecated legacy alias for ``subscribe_to_finger_state``."""
        import warnings

        warnings.warn(
            "subscribe_to_raw_eda is deprecated; use subscribe_to_finger_state instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.subscribe_to_finger_state(callback, address=address)

    async def unsubscribe_from_raw_eda(self, address: Optional[str] = None) -> None:
        """Deprecated legacy alias for ``unsubscribe_from_finger_state``."""
        import warnings

        warnings.warn(
            "unsubscribe_from_raw_eda is deprecated; use unsubscribe_from_finger_state instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.unsubscribe_from_finger_state(address=address)

    async def subscribe_to_live_eda(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Deprecated legacy alias for ``subscribe_to_raw_eda_ohms``."""
        import warnings

        warnings.warn(
            "subscribe_to_live_eda is deprecated; use subscribe_to_raw_eda_ohms instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.subscribe_to_raw_eda_ohms(callback, address=address)

    async def unsubscribe_from_live_eda(self, address: Optional[str] = None) -> None:
        """Deprecated legacy alias for ``unsubscribe_from_raw_eda_ohms``."""
        import warnings

        warnings.warn(
            "unsubscribe_from_live_eda is deprecated; use unsubscribe_from_raw_eda_ohms instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.unsubscribe_from_raw_eda_ohms(address=address)

    async def attempt_set_sample_rate(
        self,
        target_hz: int,
        address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Attempt to request ring sample-rate configuration from host side."""
        client = self._require_client(address)
        if not client:
            return {
                "ok": False,
                "status": "not-connected",
                "target_hz": int(target_hz),
                "address": (address or ""),
            }

        await asyncio.sleep(0.5)

        target_hz = min(100, max(1, int(target_hz)))
        payloads = [
            bytes([target_hz & 0xFF]),
            struct.pack("<H", target_hz),
            struct.pack("<I", target_hz),
            bytes([0x01, target_hz & 0xFF]),
            bytes([0x02, target_hz & 0xFF]),
        ]
        target_uuids = [
            self.SAMPLE_RATE_UUID,
            self.STORAGE_FORMAT_UUID,
        ]

        failures: List[str] = []
        for uuid in target_uuids:
            for payload in payloads:
                try:
                    await client.write_gatt_char(uuid, payload)
                except Exception as e:
                    failures.append(f"write {uuid} {payload.hex()}: {e}")
                    continue

                echo_hex = ""
                echoed = False
                try:
                    echo = await client.read_gatt_char(uuid)
                    echo_hex = bytes(echo).hex()
                    echoed = bytes(echo) == payload
                except Exception:
                    pass

                status = "echoed" if echoed else "written"
                return {
                    "ok": True,
                    "status": status,
                    "target_hz": target_hz,
                    "address": (address or ""),
                    "uuid": uuid,
                    "payload_hex": payload.hex(),
                    "echo_hex": echo_hex,
                }

        return {
            "ok": False,
            "status": "write-failed",
            "target_hz": target_hz,
            "address": (address or ""),
            "errors": failures,
        }

    # ── Mode & sample-rate control ──────────────────────────────────

    async def set_mode(self, mode: int, address: Optional[str] = None) -> bool:
        """Switch the ring's operational mode."""
        client = self._require_client(address)
        if not client:
            _log.warning("set_mode: not connected")
            return False
        try:
            await client.write_gatt_char(self.STORAGE_FORMAT_UUID, bytes([mode & 0xFF]))
            label = self.MODE_LABELS.get(mode & 0xFF, "unknown")
            _log.info(
                "set_mode: 0x%02X (%s) — 60s calibration begins", mode & 0xFF, label
            )
            return True
        except Exception as exc:
            _log.error("set_mode(0x%02X) failed: %s", mode & 0xFF, exc)
            return False

    async def set_sample_rate(self, hz: int, address: Optional[str] = None) -> bool:
        """Set the physiological stream sample rate via CONFIG_1."""
        client = self._require_client(address)
        if not client:
            _log.warning("set_sample_rate: not connected")
            return False
        hz = max(3, min(16, int(hz)))
        try:
            await client.write_gatt_char(self.SAMPLE_RATE_UUID, bytes([hz]))
            _log.info("set_sample_rate: %d Hz", hz)
            return True
        except Exception as exc:
            _log.error("set_sample_rate(%d) failed: %s", hz, exc)
            return False

    async def read_buffer(self, address: Optional[str] = None) -> Optional[bytes]:
        """Read the offline flash storage buffer (``7c3b82e7``)."""
        client = self._require_client(address)
        if not client:
            return None
        try:
            data = await client.read_gatt_char(self.STORAGE_UUID)
            return bytes(data)
        except Exception as exc:
            _log.debug("read_buffer: %s", exc)
            return None

    async def sync_time(
        self, timestamp_ms: Optional[int] = None, address: Optional[str] = None
    ) -> bool:
        """Synchronize real absolute time with the ring's REALTIME register."""
        client = self._require_client(address)
        if not client:
            _log.warning("sync_time: not connected")
            return False
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)
        payload = struct.pack("<Q", int(timestamp_ms))
        try:
            await client.write_gatt_char(self.REALTIME_UUID, payload)
            _log.info("sync_time: synchronized ring clock to %d ms", timestamp_ms)
            return True
        except Exception as exc:
            _log.error("sync_time failed: %s", exc)
            return False

    async def read_storage_usage(
        self, address: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Read flash storage status from the STORAGE_USAGE register."""
        client = self._require_client(address)
        if not client:
            return None
        try:
            data = await client.read_gatt_char(self.STORAGE_USAGE_UUID)
            if len(data) < 8:
                return None
            size, used = struct.unpack("<II", data[:8])
            return {
                "size_bytes": size,
                "used_bytes": used,
                "available_bytes": max(0, size - used),
                "percent_used": (used / size * 100.0) if size > 0 else 0.0,
            }
        except Exception as exc:
            _log.debug("read_storage_usage: %s", exc)
            return None

    async def rewind_storage(
        self,
        boot_count: int,
        timestamp_ms: int = 0,
        address: Optional[str] = None,
    ) -> bool:
        """Rewind internal storage reading pointer for development testing."""
        client = self._require_client(address)
        if not client:
            return False
        payload = struct.pack("<HQ", int(boot_count), int(timestamp_ms))
        try:
            await client.write_gatt_char(self.STORAGE_REWIND_UUID, payload)
            _log.info("rewind_storage: boot=%d, timestamp=%d", boot_count, timestamp_ms)
            return True
        except Exception as exc:
            _log.error("rewind_storage failed: %s", exc)
            return False

    async def send_command(self, cmd: str, address: Optional[str] = None) -> bool:
        """Send a 2-ASCII-byte command to the device COMMAND register."""
        client = self._require_client(address)
        if not client:
            return False
        if len(cmd) != 2:
            _log.error("send_command: command must be exactly 2 characters")
            return False
        try:
            await client.write_gatt_char(self.COMMAND_UUID, cmd.encode("ascii"))
            _log.info("send_command: sent '%s'", cmd)
            return True
        except Exception as exc:
            _log.error("send_command('%s') failed: %s", cmd, exc)
            return False

    async def read_storage_format(self, address: Optional[str] = None) -> Optional[int]:
        """Read the active storage format from STORAGE_FORMAT register (0, 1, or 2)."""
        client = self._require_client(address)
        if not client:
            return None
        try:
            data = await client.read_gatt_char(self.STORAGE_FORMAT_UUID)
            if data and len(data) >= 1:
                return data[0]
            return None
        except Exception as exc:
            _log.debug("read_storage_format: %s", exc)
            return None

    def _parse_storage_record(
        self, record_data: bytes, format_type: int
    ) -> Optional[Dict[str, Any]]:
        """Parse one fixed-size record from the offline flash buffer."""
        if format_type == 1:
            boot_count, timestamp_ms, eda_ohm = struct.unpack("<HQI", record_data)
            return {
                "format": "EDA",
                "boot_count": boot_count,
                "timestamp_ms": timestamp_ms,
                "eda_ohm": eda_ohm,
                "resistance_kohm": eda_ohm / 1000.0,
                "conductance_us": ((1000000.0 / eda_ohm) if eda_ohm > 0 else 0.0),
            }
        if format_type == 2:
            boot_count, timestamp_ms, srrn, srl, dne = struct.unpack(
                "<HQiii", record_data
            )
            return {
                "format": "DNE",
                "boot_count": boot_count,
                "timestamp_ms": timestamp_ms,
                "srrn": srrn,
                "srl": srl,
                "dne": dne,
            }
        return None

    async def download_storage(
        self,
        format_type: Optional[int] = None,
        address: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Download all offline recorded session records from flash memory."""
        if format_type is None:
            format_type = await self.read_storage_format(address)
            if format_type not in (1, 2):
                format_type = 2  # default to DNE format if unknown

        records = []
        item_size = 14 if format_type == 1 else 22
        buffer_bytes = bytearray()

        while True:
            chunk = await self.read_buffer(address)
            if not chunk or len(chunk) == 0:
                break
            buffer_bytes.extend(chunk)

            while len(buffer_bytes) >= item_size:
                record_data = bytes(buffer_bytes[:item_size])
                del buffer_bytes[:item_size]
                parsed = self._parse_storage_record(record_data, format_type)
                if parsed:
                    records.append(parsed)
            await asyncio.sleep(0.05)

        return records

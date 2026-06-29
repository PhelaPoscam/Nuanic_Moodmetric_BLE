import logging

_log = logging.getLogger(__name__)

"""BLE connection and device management for Nuanic ring(s).

Scanning and ring discovery live in ``_scanner.py`` (RingScanner);
this module keeps only the connection / subscription lifecycle.
"""

import asyncio
import inspect
import platform
import struct
import subprocess
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from bleak import BleakClient, BleakGATTCharacteristic

from nuanic_ring._scanner import (
    RingScanner,
    _load_last_address,
    _reset_bluetooth_radio,
    _save_last_address,
)


class NuanicConnector:
    """Handles BLE connections to one or many Nuanic/Moodmetric rings."""

    # GATT UUIDs (Verified best-fit interpretations as of 2026-06)
    STATE_UUID = "3c180fcc-bfec-4b7c-8e52-1a37f123e449"  # Off-finger / on-finger state indicator stream
    LIVE_EDA_UUID = "42dcb71b-1817-43bd-8ea3-7272780a1c9f"  # Live notify stream (currently no reliable payload)
    LIVE_DNA_UUID = "d306262b-c8c9-4c4b-9050-3a41dea706e5"  # High-rate physiological stream (raw EDA + Stress Index) at ~16Hz
    PHYSIOLOGY_UUID = LIVE_DNA_UUID
    IMU_BATCH_UUID = "468f2717-6a7d-46f9-9eb7-f92aab208bae"  # Bulk motion / IMU batch stream (14-sample batches at ~1Hz)
    SAMPLE_RATE_UUID = (
        "516b0fb6-d861-4619-9dd0-0105e8b85128"  # Writable config register (proven)
    )
    STORAGE_FORMAT_UUID = (
        "3cce21a7-e602-4e02-8c52-1e0366c1c846"  # Writable config register
    )
    BATTERY_UUID = (
        "00002a19-0000-1000-8000-00805f9b34fb"  # Standard BLE Battery Service
    )

    # Core aliases used across the existing telemetry code.
    BATTERY_CHARACTERISTIC = BATTERY_UUID
    IMU_CHARACTERISTIC = IMU_BATCH_UUID
    RAW_EDA_CHARACTERISTIC = STATE_UUID
    MYSTERY_NOTIFY_CHARACTERISTIC = LIVE_EDA_UUID
    STRESS_CHARACTERISTIC = PHYSIOLOGY_UUID

    def __init__(
        self,
        timeout: float = 7.0,
        max_scan_attempts: int = 3,
        max_connect_attempts: int = 3,
        connect_backoff_seconds: float = 2.0,
        target_address: Optional[str] = None,
        unpair_on_disconnect: bool = False,
        pair_on_connect: bool = True,
    ) -> None:
        self.max_connect_attempts = max_connect_attempts
        self.connect_backoff_seconds = connect_backoff_seconds
        self.target_address: Optional[str] = target_address
        self.unpair_on_disconnect = unpair_on_disconnect
        self.pair_on_connect = pair_on_connect
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
        """Interactive ring selection menu.

        Scans for available rings and lets the user choose which one
        to connect to.  Updates ``self.target_address`` and ``self.device``
        with the selection.

        Returns:
            str: Selected MAC address, or ``None`` if cancelled.
        """
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

        import gc

        try:
            if getattr(target_client, "is_connected", False):
                if not address:
                    self._disconnect_event.clear()
                else:
                    event = self._disconnect_events.get(address.upper())
                    if event:
                        event.clear()

                for char_uuid in [
                    self.STRESS_CHARACTERISTIC,
                    self.IMU_CHARACTERISTIC,
                    self.RAW_EDA_CHARACTERISTIC,
                    self.MYSTERY_NOTIFY_CHARACTERISTIC,
                ]:
                    try:
                        await target_client.stop_notify(char_uuid)
                    except Exception:
                        pass

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
            if platform.system() == "Windows":
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

            gc.collect()
            await asyncio.sleep(0.5)

    async def connect(self):
        """Connect to Nuanic ring with automatic retry and recovery."""
        await self._cleanup_client()
        if not self.target_address:
            selected = await self.select_ring_interactive()
            if not selected:
                print("[FAIL] No ring selected\n")
                return False
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

                self.clients[address] = client

                if device is not None:
                    self.devices[address] = device

                self.client = client
                self.device = device
                self.target_address = address
                _save_last_address(address)
                return True
            except asyncio.CancelledError:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise
            except Exception as e:
                print(
                    f"[CONN-FAIL] {address} attempt {attempt}/{self.max_connect_attempts}: {type(e).__name__}: {e}"
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if attempt < self.max_connect_attempts:
                    await asyncio.sleep(self.connect_backoff_seconds)

        return False

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

    async def disconnect(self, address: Optional[str] = None) -> None:
        """Disconnect from ring.

        By default this keeps OS-level pairing intact. Set
        unpair_on_disconnect=True on connector init if you explicitly want
        forced unpair for troubleshooting.
        """
        if address:
            address = address.upper()
            if address in self.clients:
                try:
                    await self._cleanup_client(address)
                except Exception:
                    pass
                finally:
                    self.devices.pop(address, None)
                print(f"[OK] Disconnected {address}")
        else:
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
                print("[OK] Disconnected")
            else:
                print("[INFO] No active BLE connection to close")

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
            value = await client.read_gatt_char(self.BATTERY_CHARACTERISTIC)
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

    async def subscribe_to_stress(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to stress data notifications"""
        return await self._subscribe(
            self.STRESS_CHARACTERISTIC, callback, address, "stress data"
        )

    async def subscribe_to_imu(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to IMU (accelerometer) notifications"""
        return await self._subscribe(
            self.IMU_CHARACTERISTIC, callback, address, "IMU data"
        )

    async def unsubscribe_from_stress(self, address: Optional[str] = None) -> None:
        """Unsubscribe from stress notifications"""
        await self._unsubscribe(self.STRESS_CHARACTERISTIC, address)

    async def unsubscribe_from_imu(self, address: Optional[str] = None) -> None:
        """Unsubscribe from IMU notifications"""
        await self._unsubscribe(self.IMU_CHARACTERISTIC, address)

    async def subscribe_to_raw_eda(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to raw EDA data notifications"""
        return await self._subscribe(
            self.RAW_EDA_CHARACTERISTIC, callback, address, "raw EDA data"
        )

    async def unsubscribe_from_raw_eda(self, address: Optional[str] = None) -> None:
        """Unsubscribe from raw EDA notifications"""
        await self._unsubscribe(self.RAW_EDA_CHARACTERISTIC, address)

    async def subscribe_to_live_eda(
        self,
        callback: Callable[
            [BleakGATTCharacteristic, bytearray], Union[Awaitable[None], None]
        ],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to LIVE_EDA UUID notifications (42dcb71b...)."""
        return await self._subscribe(
            self.MYSTERY_NOTIFY_CHARACTERISTIC,
            callback,
            address,
            "LIVE_EDA notifications",
        )

    async def unsubscribe_from_live_eda(self, address: Optional[str] = None) -> None:
        """Unsubscribe from LIVE_EDA UUID notifications."""
        await self._unsubscribe(self.MYSTERY_NOTIFY_CHARACTERISTIC, address)

    async def attempt_set_sample_rate(
        self,
        target_hz: int,
        address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Attempt to request ring sample-rate configuration from host side."""
        client = self.get_client(address)
        if not client or not getattr(client, "is_connected", False):
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

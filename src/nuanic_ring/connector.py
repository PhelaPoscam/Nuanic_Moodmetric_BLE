"""BLE connection and device management for Nuanic ring(s)."""

import asyncio
import inspect
import platform
import struct
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bleak import BleakClient, BleakScanner

# Persists the last-used ring MAC so we can reconnect even when a stale
# OS-level connection prevents the ring from advertising.
_ADDR_CACHE_FILE = Path(__file__).parents[3] / "data" / ".last_ring_addr"


class NuanicConnector:
    """Handles BLE connections to one or many Nuanic/Moodmetric rings."""

    # GATT UUIDs (Verified best-fit interpretations as of 2026-04)
    STATE_UUID = "3c180fcc-bfec-4b7c-8e52-1a37f123e449"  # Off-finger / on-finger state indicator stream
    STORAGE_UUID = "7c3b82e7-22b7-4cb6-8458-ba325edf6ede"  # Historical storage / buffer characteristic
    LIVE_EDA_UUID = "42dcb71b-1817-43bd-8ea3-7272780a1c9f"  # Live notify stream (currently no reliable payload)
    LIVE_DNA_UUID = "d306262b-c8c9-4c4b-9050-3a41dea706e5"  # High-rate motion / physiology stream (IMU/EDM)
    SET_TIME_UUID = (
        "dc9c31a7-fbd3-467a-8777-10900c423d3b"  # Writable config / timestamp register
    )
    SAMPLE_RATE_UUID = "516b0fb6-d861-4619-9dd0-0105e8b85128"  # Writable config register (rate-write effect unproven)
    STORAGE_FORMAT_UUID = (
        "3cce21a7-e602-4e02-8c52-1e0366c1c846"  # Writable config register
    )
    BATTERY_UUID = (
        "00002a19-0000-1000-8000-00805f9b34fb"  # Standard BLE Battery Service
    )

    # Backward-compatible aliases used across the existing telemetry code.
    BATTERY_CHARACTERISTIC = BATTERY_UUID
    IMU_CHARACTERISTIC = LIVE_DNA_UUID
    STATE_CHARACTERISTIC = STATE_UUID
    RAW_EDA_CHARACTERISTIC = STATE_CHARACTERISTIC
    MYSTERY_NOTIFY_CHARACTERISTIC = LIVE_EDA_UUID

    def __init__(
        self,
        timeout=15.0,
        max_scan_attempts=3,
        max_connect_attempts=3,
        connect_backoff_seconds=2.0,
        target_address=None,
        unpair_on_disconnect=False,
        pair_on_connect=True,
    ):
        self.timeout = timeout
        self.max_scan_attempts = max_scan_attempts
        self.max_connect_attempts = max_connect_attempts
        self.connect_backoff_seconds = connect_backoff_seconds
        self.target_address = (
            target_address  # BLE address to connect to (e.g., "AA:BB:CC:DD:EE:FF")
        )
        self.unpair_on_disconnect = unpair_on_disconnect
        self.pair_on_connect = pair_on_connect
        self.client = None
        self.device = None
        self._disconnect_event = asyncio.Event()

        # Multi-device runtime registries keyed by BLE MAC address.
        self.clients = {}
        self.devices = {}
        self._disconnect_events = {}

    def _on_disconnect(self, _client):
        """Bleak disconnect callback to confirm OS-level link release."""
        self._disconnect_event.set()
        print("[DISC] BLE disconnect callback fired")

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

    # ------------------------------------------------------------------
    # Address cache - lets us reconnect directly when the ring is bonded
    # to Windows but not advertising (stale connection scenario).
    # ------------------------------------------------------------------

    def _save_last_address(self, address: str) -> None:
        try:
            _ADDR_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _ADDR_CACHE_FILE.write_text(address.strip())
        except Exception:
            pass

    def _load_last_address(self) -> str:
        try:
            if _ADDR_CACHE_FILE.exists():
                return _ADDR_CACHE_FILE.read_text().strip() or ""
        except Exception:
            pass
        return ""

    async def _reset_bluetooth_radio(self) -> bool:
        """Toggle the Windows Bluetooth radio off/on (Windows only).

        This is the programmatic equivalent of flipping Bluetooth off and on
        in Windows Settings.  It flushes all stale ACL connections, including
        rings left "connected" from a previous crashed/killed session.  After
        the reset the ring resumes advertising and the next scan succeeds.
        """
        if platform.system() != "Windows":
            return False
        try:
            import winrt.windows.devices.radios as radios_winrt  # type: ignore

            all_radios = await radios_winrt.Radio.get_radios_async()
            bt_radio = next(
                (r for r in all_radios if r.kind == radios_winrt.RadioKind.BLUETOOTH),
                None,
            )
            if not bt_radio:
                print("[BT-RESET] No Bluetooth radio found.")
                return False

            if bt_radio.state != radios_winrt.RadioState.OFF:
                print("[BT-RESET] Turning Bluetooth off...", end=" ", flush=True)
                await bt_radio.set_state_async(radios_winrt.RadioState.OFF)
                await asyncio.sleep(1.5)
            else:
                print("[BT-RESET] Bluetooth is already off...", end=" ", flush=True)

            if bt_radio.state != radios_winrt.RadioState.ON:
                print("turning on...", end=" ", flush=True)
                await bt_radio.set_state_async(radios_winrt.RadioState.ON)
                await asyncio.sleep(2.5)  # give stack time to re-initialize
            print("[OK]")
            return True
        except Exception as e:
            print(f"[BT-RESET] Could not reset radio: {e}")
            return False

    async def _winrt_force_close(self, address: str) -> None:
        """Dispose the WinRT BluetoothLEDevice handle (Windows only).

        After BleakClient.disconnect() the WinRT device object can still
        hold the OS-level ACL link open, which leaves the ring in a
        'connected' state.  Closing the handle tells the OS (and the ring)
        the connection is gone so the ring resumes advertising.
        """
        if platform.system() != "Windows":
            return
        try:
            import winrt.windows.devices.bluetooth as bt_winrt  # type: ignore

            addr_int = int(address.replace(":", ""), 16)
            device = await bt_winrt.BluetoothLEDevice.from_bluetooth_address_async(
                addr_int
            )
            if device:
                device.close()
                print("[CLEANUP] WinRT device handle closed.")
                await asyncio.sleep(0.3)
        except Exception:
            pass

    def _create_bleak_client(self, target, disconnected_callback=None):
        """Create BleakClient with robust Windows-friendly arguments.

        Uses pair=... when available and gracefully falls back for older
        Bleak versions that do not support that constructor argument.
        """
        kwargs = {
            "timeout": self.timeout,
            "disconnected_callback": disconnected_callback or self._on_disconnect,
        }

        # Windows-specific tweaks to help avoid zombie connections and cache issues
        if platform.system() == "Windows":
            # Using winrt backend directly avoids some OS caching layers if available
            kwargs["use_cached_services"] = False

        try:
            params = inspect.signature(BleakClient).parameters
            if "pair" in params:
                kwargs["pair"] = self.pair_on_connect
        except Exception:
            # Signature probing can fail on some backends; keep safe defaults.
            pass

        return BleakClient(target, **kwargs)

    async def find_device(self):
        """Scan for Nuanic ring.
        If target_address is set, search for that specific device.
        Retries automatically as part of discovery process.
        """
        search_label = (
            f"'{self.target_address}'" if self.target_address else "(any Nuanic)"
        )

        for attempt in range(1, self.max_scan_attempts + 1):
            try:
                # Quick scan - find all devices
                devices = await BleakScanner.discover(timeout=2.0)

                # Filter Nuanic / Moodmetric devices
                for device in devices:
                    if not device.name or (
                        "Nuanic" not in device.name and "Moodmetric" not in device.name
                    ):
                        continue

                    # If target address specified, only match that one
                    if self.target_address:
                        if device.address.lower() == self.target_address.lower():
                            self.device = device
                            return device
                    else:
                        # No target specified, accept first available
                        self.device = device
                        return device

                # Not found in this scan
                if attempt < self.max_scan_attempts:
                    await asyncio.sleep(0.5)  # Short pause between scans

            except asyncio.CancelledError:
                # Re-raise so Ctrl+C still works
                raise
            except Exception as e:
                if attempt < self.max_scan_attempts:
                    await asyncio.sleep(0.5)

        return None

    def _sanitize_address(self, addr: str) -> str:
        """Ensure MAC address is clean hex/colons. Removes Unicode corruption."""
        if not addr:
            return ""
        # Keep only hex digits and colons. Strip everything else.
        import re

        clean = re.sub(r"[^0-9A-Fa-f:]", "", addr)
        return clean.upper()

    def _sanitize_name(self, name: str) -> str:
        """Ensure name contains only ASCII characters for safe terminal rendering."""
        if not name:
            return ""
        return "".join(c for c in name if ord(c) < 128)

    async def list_available_rings(
        self,
        include_device: bool = False,
        scan_timeout: float = 5.0,
        attempts: int = 2,
        retry_delay: float = 1.0,
        stop_if_found: bool = False,
    ):
        """Scan and return list of all available Nuanic rings."""
        if not stop_if_found:
            print(
                f"[SCAN] Discovering Nuanic rings (timeout: {scan_timeout}s, attempts: {attempts})..."
            )

        from nuanic_ring.ring_profiles import (
            NUANIC_SERVICE_UUID,
            MOODMETRIC_SERVICE_UUIDS,
        )

        merged = {}
        try:
            for attempt in range(1, max(1, attempts) + 1):
                if attempts > 1 and not stop_if_found:
                    print(f"[SCAN] Attempt {attempt}/{attempts}...")

                # Use discover(return_adv=True) for a thorough window
                devices_map = await BleakScanner.discover(
                    timeout=max(2.0, scan_timeout), return_adv=True
                )

                for device, adv in devices_map.values():
                    name = device.name or ""
                    clean_name = self._sanitize_name(name)

                    adv_uuids = [u.lower() for u in adv.service_uuids]

                    is_nuanic = (
                        "Nuanic" in name or NUANIC_SERVICE_UUID.lower() in adv_uuids
                    )
                    is_moodmetric = "Moodmetric" in name or any(
                        u.lower() in adv_uuids for u in MOODMETRIC_SERVICE_UUIDS
                    )

                    if is_nuanic or is_moodmetric:
                        addr_raw = device.address or ""
                        addr = self._sanitize_address(addr_raw)

                        if not addr:
                            continue

                        entry = {
                            "address": addr,
                            "name": (
                                clean_name
                                if clean_name
                                else ("Nuanic" if is_nuanic else "Moodmetric")
                            ),
                        }
                        if include_device:
                            entry["device"] = device
                        merged[addr] = entry

                if stop_if_found and merged:
                    break

                if attempt < attempts:
                    await asyncio.sleep(retry_delay)

            return list(merged.values())

        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                print(f"[WARN] Scan error: {e}")
            return []

    def _get_windows_paired_rings(self):
        """Return paired Nuanic/Moodmetric rings from Windows PnP records.

        This helps when rings are connected in Windows but not currently visible
        in active BLE advertisements.
        """
        if platform.system() != "Windows":
            return []

        ps_cmd = (
            "$rows = Get-PnpDevice -Class Bluetooth "
            "| Where-Object { $_.FriendlyName -match 'Nuanic|Moodmetric' -or $_.InstanceId -match 'BTHLE\\\\DEV_' }; "
            "foreach ($r in $rows) { "
            "  $addr = ''; "
            "  try { $addr = (Get-PnpDeviceProperty -InstanceId $r.InstanceId -KeyName 'DEVPKEY_Bluetooth_DeviceAddress').Data } catch {} ; "
            "  Write-Output ($r.FriendlyName + '|' + $addr + '|' + $r.InstanceId + '|' + $r.Status) "
            "}"
        )

        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return []
            rings = []
            for line in completed.stdout.splitlines():
                if not line or "|" not in line:
                    continue
                parts = line.split("|", 3)
                if len(parts) != 4:
                    continue

                name_raw = (parts[0] or "").strip()
                addr_raw = (parts[1] or "").strip().upper()
                if not name_raw:
                    continue
                if (
                    "NUANIC" not in name_raw.upper()
                    and "MOODMETRIC" not in name_raw.upper()
                ):
                    continue

                name = self._sanitize_name(name_raw)
                addr = self._sanitize_address(addr_raw)

                if not addr:
                    continue

                # Standardize 12-char paired addresses into XX:XX:XX...
                if len(addr) == 12 and ":" not in addr:
                    addr = ":".join(addr[i : i + 2] for i in range(0, 12, 2))

                rings.append(
                    {"address": addr, "name": name, "source": "windows-paired"}
                )

            # Deduplicate by address
            dedup = {}
            for ring in rings:
                dedup[ring["address"]] = ring
            return list(dedup.values())
        except Exception:
            return []

    async def list_available_rings_with_paired(
        self,
        scan_timeout: float = 6.0,
        attempts: int = 3,
    ):
        """Return discoverable rings plus Windows paired rings (if any)."""
        scanned = await self.list_available_rings(
            include_device=True,
            scan_timeout=scan_timeout,
            attempts=attempts,
            retry_delay=1.0,
        )
        paired = self._get_windows_paired_rings()

        merged = {}
        # Scanned results take priority because they contain active BleakDevice objects
        for ring in scanned:
            addr = self._sanitize_address(ring["address"])
            name = self._sanitize_name(ring["name"])
            merged[addr.upper()] = {
                "address": addr,
                "name": name,
                "device": ring.get("device"),
                "source": "scan",
            }

        for ring in paired:
            addr = self._sanitize_address(ring["address"])
            name = self._sanitize_name(ring["name"])
            key = addr.upper()
            if key not in merged:
                merged[key] = {
                    "address": addr,
                    "name": name,
                    "device": None,
                    "source": "windows-paired",
                }

        return list(merged.values())

    async def select_ring_interactive(self):
        """Interactive ring selection menu.

        NOTE: This is called automatically by connect() if no target_address is set.
        No need to call this manually unless you want to select before connecting.

        Scans for available rings and lets user choose which one to connect to.
        Updates self.target_address with the selected ring's MAC.

        Returns:
            str: Selected MAC address, or None if cancelled
        """
        print("\n" + "=" * 60)
        print("RING SELECTION")
        print("=" * 60)

        # Pairing-mode rings can advertise intermittently. Include Windows paired
        # records so users can still select already-connected rings.
        rings = await self.list_available_rings_with_paired()

        if not rings:
            print("[!] No Nuanic rings found.")

            # The ring is most likely still 'connected' to Windows from a
            # previous session.  Reset the BT radio (=toggle off/on) to flush
            # the stale ACL link, then rescan once.
            print(
                "[BT-RESET] Stale connection detected - resetting "
                "Bluetooth adapter..."
            )
            reset_ok = await self._reset_bluetooth_radio()
            if reset_ok:
                print("[BT-RESET] Rescanning after radio reset...")
                rings = await self.list_available_rings_with_paired()

            if not rings:
                # Radio toggle didn't help (or not on Windows).
                # Fall back to direct connect using the cached address.
                cached = self._load_last_address()
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

        # Multiple rings - let user choose
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

    async def check_mac_address_dynamic(
        self, num_scans: int = 5, delay_between_scans: float = 1.0
    ) -> dict:
        """Check if the ring has a dynamic or static MAC address.

        Performs multiple scans and compares MAC addresses to determine if the device
        uses a dynamic (changing) or static (constant) MAC address.

        Args:
            num_scans: Number of scans to perform (default: 5)
            delay_between_scans: Delay in seconds between scans (default: 1.0)

        Returns:
            dict with keys:
                - 'is_dynamic': bool, True if MAC address is dynamic, False if static
                - 'addresses': list of discovered MAC addresses
                - 'unique_addresses': set of unique MAC addresses
                - 'scans_performed': number of scans performed
                - 'num_unique': number of unique addresses found
                - 'confidence': str, 'high' if clear pattern, 'low' if inconclusive
        """
        print(
            f"\n[CHECK] Scanning for MAC address changes ({num_scans} scans, {delay_between_scans}s delay)...\n"
        )

        discovered_addresses = []

        try:
            for scan_num in range(1, num_scans + 1):
                print(f"[SCAN {scan_num}/{num_scans}]", end=" ", flush=True)

                try:
                    devices = await BleakScanner.discover(timeout=3.0)

                    # Find Nuanic / Moodmetric devices
                    nuanic_found = False
                    for device in devices:
                        if device.name and (
                            "Nuanic" in device.name or "Moodmetric" in device.name
                        ):
                            # If target address specified, only record that one
                            if self.target_address:
                                if (
                                    device.address.lower()
                                    == self.target_address.lower()
                                ):
                                    discovered_addresses.append(device.address)
                                    print(f"Found: {device.address} ({device.name})")
                                    nuanic_found = True
                                    break
                            else:
                                # Record first available Nuanic device
                                discovered_addresses.append(device.address)
                                print(f"Found: {device.address} ({device.name})")
                                nuanic_found = True
                                break

                    if not nuanic_found:
                        print("Not found in this scan")

                except Exception as e:
                    print(f"Scan error: {e}")

                # Wait before next scan
                if scan_num < num_scans:
                    await asyncio.sleep(delay_between_scans)

            # Analyze results
            unique_addresses = list(set(discovered_addresses))
            is_dynamic = len(unique_addresses) > 1

            # Confidence assessment
            if not discovered_addresses:
                confidence = "low"  # No device found
            elif len(unique_addresses) == 1:
                confidence = "high"  # All scans found same address
            else:
                confidence = (
                    "high" if len(discovered_addresses) >= num_scans - 1 else "low"
                )  # Require mostly successful scans for high confidence

            print(f"\n[RESULT]")
            print(f"  Unique addresses found: {len(unique_addresses)}")
            print(f"  Addresses: {unique_addresses}")
            print(f"  MAC is {'DYNAMIC' if is_dynamic else 'STATIC'}")
            print(f"  Confidence: {confidence}\n")

            return {
                "is_dynamic": is_dynamic,
                "addresses": discovered_addresses,
                "unique_addresses": unique_addresses,
                "scans_performed": num_scans,
                "num_unique": len(unique_addresses),
                "confidence": confidence,
            }

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"\n[ERROR] Check failed: {e}")
            return {
                "is_dynamic": None,
                "addresses": discovered_addresses,
                "unique_addresses": list(set(discovered_addresses)),
                "scans_performed": len(discovered_addresses),
                "num_unique": len(set(discovered_addresses)),
                "confidence": "low",
            }

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

                # Explicitly attempt to stop notifications before disconnecting
                # to help Windows clear the GATT cache cleanly.
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

                # Wait explicitly for the disconnected_callback to fire
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
            # FORCE cleanup for Windows ghost connections:
            if platform.system() == "Windows":
                try:
                    # Explicitly close internal WinRT handles to drop the ACL link
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

            # Break circular reference (self -> client -> disconnected_callback -> self)
            try:
                target_client.set_disconnected_callback(None)
            except Exception:
                pass

            if not address:
                self.client = None
            else:
                self.clients.pop(address.upper(), None)

            # Force garbage collector to release lingering COM objects before process exits
            gc.collect()
            await asyncio.sleep(
                0.5
            )  # Give Windows driver time to process the handle closure

    async def connect(self):
        """Connect to Nuanic ring with automatic retry and recovery.

        If no target_address is set, shows interactive menu to select ring.

        Connection Flow:
        1. If needed, let user select which ring to connect to
        2. Scan for device (with retries)
        3. Establish BLE connection
        4. Perform pairing (if needed)
        5. Return success
        """
        await self._cleanup_client()

        # If no target address specified, prompt user to select
        if not self.target_address:
            selected = await self.select_ring_interactive()
            if not selected:
                print("[FAIL] No ring selected\n")
                return False

        search_label = (
            f"'{self.target_address}'" if self.target_address else "(any available)"
        )
        print(f"[INIT] Connecting to Nuanic ring {search_label}...")

        # Connection logic is wrapped in a try...finally to ensure absolute safety
        # against crashes leaving zombie connections open.
        try:
            # If a concrete device object was selected from the latest scan, try it first.
            # This avoids a second scan/match cycle that can fail when BLE private MAC rotates.
            if self.device and (
                not self.target_address
                or self.device.address.lower() == self.target_address.lower()
            ):
                print("[CONN] Trying selected device directly...", end=" ", flush=True)
                try:
                    self._disconnect_event.clear()
                    self.client = self._create_bleak_client(self.device)
                    await self.client.connect()
                    print("[OK] Connected")

                    if self.pair_on_connect:
                        print("[PAIR] Requested via BleakClient(pair=True)")
                    else:
                        print("[PAIR] Establishing encryption...", end=" ", flush=True)
                        try:
                            await self.client.pair()
                            print("[OK] Paired")
                        except Exception:
                            print("[INFO] Pairing not available")

                    if not getattr(self.client, "is_connected", False):
                        print("[RETRY] Link dropped right after connect")
                        await self._cleanup_client()
                    else:
                        print("\n[OK] Connection established!\n")
                        address = str(self.client.address).upper()
                        self.clients[address] = self.client
                        if self.device is not None:
                            self.devices[address] = self.device
                        self._save_last_address(self.client.address)
                        return True
                except Exception as e:
                    print(f"[RETRY] {e}")
                    await self._cleanup_client()

            for attempt in range(1, self.max_connect_attempts + 1):
                # Step 1: Find device via scan
                print(
                    f"\n[SCAN {attempt}/{self.max_connect_attempts}] Searching for device...",
                    end=" ",
                    flush=True,
                )
                scan_ok = await self.find_device()
                if not scan_ok:
                    print("[NOT FOUND]")
                    if self.target_address and attempt == 1:
                        print(
                            "[HINT] Device not in scan results — may already be bonded to Windows "
                            "or using a rotating MAC. Trying direct address connection..."
                        )

                # Step 2: Connect — use scanned device object when available, otherwise
                # connect by address string directly (works for Windows-bonded devices
                # that are invisible to BLE scan).
                connect_target = self.device if scan_ok else self.target_address
                if connect_target is None:
                    if attempt < self.max_connect_attempts:
                        print(f"[WAIT] Pausing before retry...")
                        await asyncio.sleep(self.connect_backoff_seconds)
                    continue

                if scan_ok:
                    print(f"[OK] Found: {self.device.name}")

                print(
                    f"[CONN {attempt}/{self.max_connect_attempts}] Connecting to BLE device...",
                    end=" ",
                    flush=True,
                )
                try:
                    self._disconnect_event.clear()
                    self.client = self._create_bleak_client(connect_target)
                    await self.client.connect()
                    print("[OK] Connected")
                except asyncio.TimeoutError:
                    print(f"[TIMEOUT] ({self.timeout}s)")
                    await self._cleanup_client()
                    if attempt < self.max_connect_attempts:
                        print(f"[WAIT] Resetting BLE and retrying...")
                        await asyncio.sleep(self.connect_backoff_seconds)
                    continue
                except Exception as e:
                    print(f"[ERROR] {e}")
                    await self._cleanup_client()
                    if attempt < self.max_connect_attempts:
                        print(f"[WAIT] Resetting BLE and retrying...")
                        await asyncio.sleep(self.connect_backoff_seconds)
                    continue

                # Step 3: Pair (optional)
                if self.pair_on_connect:
                    print(
                        f"[PAIR {attempt}/{self.max_connect_attempts}] Requested via BleakClient(pair=True)"
                    )
                else:
                    print(
                        f"[PAIR {attempt}/{self.max_connect_attempts}] Establishing encryption...",
                        end=" ",
                        flush=True,
                    )
                    try:
                        await self.client.pair()
                        print("[OK] Paired")
                    except Exception as e:
                        # Pairing may fail if already paired - this is OK
                        print(f"[INFO] Pairing not available")

                if not getattr(self.client, "is_connected", False):
                    print("[RETRY] Link dropped right after connect")
                    await self._cleanup_client()
                    if attempt < self.max_connect_attempts:
                        print(f"[WAIT] Retrying after disconnect...")
                        await asyncio.sleep(self.connect_backoff_seconds)
                    continue

                # Success!
                print(f"\n[OK] Connection established!\n")
                address = str(self.client.address).upper()
                self.clients[address] = self.client
                if self.device is not None:
                    self.devices[address] = self.device
                self._save_last_address(self.client.address)
                return True

            # All attempts failed
            print(
                f"\n[FAIL] Could not connect after {self.max_connect_attempts} attempts\n"
            )
            return False

        except KeyboardInterrupt:
            # Explicitly catch KeyboardInterrupt to ensure cleanup fires gracefully
            print("\n[INFO] Connect aborted by user.")
            await self._cleanup_client()
            raise

    async def discover_all_matching_rings(
        self,
        include_device: bool = True,
        scan_timeout: float = 4.0,
        attempts: int = 2,
        retry_delay: float = 0.5,
        stop_if_found: bool = False,
    ) -> List[Dict[str, Any]]:
        """Discover all visible Nuanic/Moodmetric rings."""
        scanned = await self.list_available_rings(
            include_device=include_device,
            scan_timeout=scan_timeout,
            attempts=attempts,
            retry_delay=retry_delay,
            stop_if_found=stop_if_found,
        )

        # On Windows, merge in paired records to catch bonded devices that do not
        # advertise reliably during a short scan window.
        if platform.system() != "Windows":
            return scanned

        paired = self._get_windows_paired_rings()
        merged = {entry["address"].upper(): entry for entry in scanned}
        for entry in paired:
            key = entry["address"].upper()
            if key not in merged:
                merged[key] = {
                    "address": entry["address"],
                    "name": entry.get("name") or "Nuanic",
                    "device": None,
                }

        return list(merged.values())

    async def connect_device(self, address: str, device: Any = None) -> bool:
        """Connect one device and register it in the multi-device registry."""
        address = address.upper()
        event = self._disconnect_events.setdefault(address, asyncio.Event())
        event.clear()

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

                # Keep legacy single-device fields aligned with the most recent connect.
                self.client = client
                self.device = device
                self.target_address = address
                self._save_last_address(address)
                return True
            except Exception as e:
                print(
                    f"[CONN-FAIL] {address} attempt {attempt}/{self.max_connect_attempts}: {e}"
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
        """Connect to many rings with staggered timing to avoid adapter overload.

        Returns:
            dict: mapping {address: bool}
        """
        results = {}

        # Build target list from discovery when explicit addresses are not provided.
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
                    # self.clients.pop happens in _cleanup_client
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
                    # self.clients.pop happens in _cleanup_client

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
        """Remove device from Windows Bluetooth pairing.
        Uses Windows PowerShell to safely remove the pairing.
        """
        if not self.device:
            return

        try:
            # Convert BLE address to Windows format (remove colons)
            ble_address = self.device.address.replace(":", "")

            # PowerShell command to remove Bluetooth device
            ps_cmd = (
                f"Remove-Item -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\BTHPORT\\Parameters\\Keys\\*\\{ble_address}' "
                "-Force -ErrorAction SilentlyContinue; "
                f"Get-PnpDevice -FriendlyName '*{self.device.name}*' | Remove-PnpDevice -Force -ErrorAction SilentlyContinue"
            )

            # Run PowerShell command
            process = subprocess.Popen(
                ["powershell", "-Command", ps_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = process.communicate(timeout=5)

            if (
                process.returncode == 0 or process.returncode == 1
            ):  # 1 = item not found (already unpaired)
                print(f"[OK] Removed {self.device.name} from Windows Bluetooth")
            else:
                print(
                    f"[WARN] Unpair: {stderr.decode().strip() if stderr else 'Unknown error'}"
                )

        except subprocess.TimeoutExpired:
            print("[WARN] Unpair timeout")
        except Exception as e:
            print(f"[WARN] Unpair error: {e}")

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

    async def subscribe_to_stress(
        self,
        callback: Callable[[Any, bytes], None],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to stress data notifications"""
        client = self.clients.get(address.upper()) if address else self.client
        if not client:
            print("[FAIL] Subscription error: No client")
            return False

        if not client.is_connected:
            print("[FAIL] Subscription error: Not connected")
            return False

        try:
            await client.start_notify(self.STRESS_CHARACTERISTIC, callback)
            print("[OK] Subscribed to stress data")
            return True
        except Exception as e:
            print(f"[FAIL] Subscription error: {e}")
            return False

    async def subscribe_to_imu(
        self,
        callback: Callable[[Any, bytes], None],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to IMU (accelerometer) notifications"""
        client = self.clients.get(address.upper()) if address else self.client
        if not client:
            print("[FAIL] IMU subscription error: No client")
            return False

        if not client.is_connected:
            print("[FAIL] IMU subscription error: Not connected")
            return False

        try:
            await client.start_notify(self.IMU_CHARACTERISTIC, callback)
            print("[OK] Subscribed to IMU data")
            return True
        except Exception as e:
            print(f"[FAIL] IMU subscription error: {e}")
            return False

    async def unsubscribe_from_stress(
        self,
        address: Optional[str] = None,
    ) -> None:
        """Unsubscribe from stress notifications"""
        if address:
            client = self.clients.get(address.upper())
        else:
            client = self.client

        if client:
            try:
                await client.stop_notify(self.STRESS_CHARACTERISTIC)
            except:
                pass

    async def unsubscribe_from_imu(
        self,
        address: Optional[str] = None,
    ) -> None:
        """Unsubscribe from IMU notifications"""
        if address:
            client = self.clients.get(address.upper())
        else:
            client = self.client

        if client:
            try:
                await client.stop_notify(self.IMU_CHARACTERISTIC)
            except:
                pass

    async def subscribe_to_raw_eda(
        self,
        callback: Callable[[Any, bytes], None],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to raw EDA data notifications"""
        client = self.clients.get(address.upper()) if address else self.client
        if not client:
            print("[FAIL] Subscription error: No client")
            return False

        if not client.is_connected:
            print("[FAIL] Subscription error: Not connected")
            return False

        try:
            await client.start_notify(self.RAW_EDA_CHARACTERISTIC, callback)
            print("[OK] Subscribed to raw EDA data")
            return True
        except Exception as e:
            print(f"[FAIL] Subscription error: {e}")
            return False

    async def unsubscribe_from_raw_eda(
        self,
        address: Optional[str] = None,
    ) -> None:
        """Unsubscribe from raw EDA notifications"""
        if address:
            client = self.clients.get(address.upper())
        else:
            client = self.client

        if client:
            try:
                await client.stop_notify(self.RAW_EDA_CHARACTERISTIC)
            except:
                pass

    async def subscribe_to_live_eda(
        self,
        callback: Callable[[Any, bytes], None],
        address: Optional[str] = None,
    ) -> bool:
        """Subscribe to LIVE_EDA UUID notifications (42dcb71b...)."""
        client = self.clients.get(address.upper()) if address else self.client
        if not client:
            print("[FAIL] LIVE_EDA subscription error: No client")
            return False

        if not client.is_connected:
            print("[FAIL] LIVE_EDA subscription error: Not connected")
            return False

        try:
            await client.start_notify(self.MYSTERY_NOTIFY_CHARACTERISTIC, callback)
            print("[OK] Subscribed to LIVE_EDA notifications")
            return True
        except Exception as e:
            print(f"[FAIL] LIVE_EDA subscription error: {e}")
            return False

    async def unsubscribe_from_live_eda(
        self,
        address: Optional[str] = None,
    ) -> None:
        """Unsubscribe from LIVE_EDA UUID notifications."""
        if address:
            client = self.clients.get(address.upper())
        else:
            client = self.client

        if client:
            try:
                await client.stop_notify(self.MYSTERY_NOTIFY_CHARACTERISTIC)
            except:
                pass

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

    async def attempt_set_sample_rate(
        self,
        target_hz: int,
        address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Attempt to request ring sample-rate configuration from host side.

        Notes:
        - A successful write or echo means transport-level success only.
        - Firmware may still ignore the requested rate behaviorally.
        """
        client = self.get_client(address)
        if not client or not getattr(client, "is_connected", False):
            return {
                "ok": False,
                "status": "not-connected",
                "target_hz": int(target_hz),
                "address": (address or ""),
            }

        # Give the ring stack a moment to settle after connection
        await asyncio.sleep(0.5)

        target_hz = max(1, int(target_hz))
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

"""BLE scanning and ring discovery — extracted from connector.py.

ponytail: pure scanning, no connection state. Called by NuanicConnector
which keeps the connection/subscription lifecycle.
"""

import asyncio
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

from bleak import BleakScanner

from nuanic_ring.ring_profiles import (
    MOODMETRIC_SERVICE_UUIDS,
    NUANIC_SERVICE_UUID,
)

# Persists the last-used ring MAC so we can reconnect even when a stale
# OS-level connection prevents the ring from advertising.
_ADDR_CACHE_FILE = Path.home() / ".nuanic_ring" / ".last_ring_addr"


# ---------------------------------------------------------------------------
# Module-level helpers — no instance state needed
# ---------------------------------------------------------------------------


def _sanitize_address(addr: str) -> str:
    """Ensure MAC address is clean hex/colons. Removes Unicode corruption."""
    if not addr:
        return ""
    clean = re.sub(r"[^0-9A-Fa-f:]", "", addr)
    return clean.upper()


def _sanitize_name(name: str) -> str:
    """Ensure name contains only ASCII characters for safe terminal rendering."""
    if not name:
        return ""
    return "".join(c for c in name if ord(c) < 128)


def _save_last_address(address: str) -> None:
    try:
        _ADDR_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ADDR_CACHE_FILE.write_text(address.strip())
    except Exception:
        pass


def _load_last_address() -> str:
    try:
        if _ADDR_CACHE_FILE.exists():
            return _ADDR_CACHE_FILE.read_text().strip() or ""
    except Exception:
        pass
    return ""


async def _reset_bluetooth_radio() -> bool:
    """Toggle the Windows Bluetooth radio off/on (Windows only).

    Flushes stale ACL connections so the ring resumes advertising.
    """
    if platform.system() != "Windows":
        return False
    try:
        import winrt.windows.devices.radios as radios_winrt

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
            await asyncio.sleep(2.5)
        print("[OK]")
        return True
    except Exception as e:
        print(f"[BT-RESET] Could not reset radio: {e}")
        return False


def _get_windows_paired_rings() -> list[dict]:
    """Return paired Nuanic/Moodmetric rings from Windows PnP records."""
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

            name = _sanitize_name(name_raw)
            addr = _sanitize_address(addr_raw)

            if not addr:
                continue

            if len(addr) == 12 and ":" not in addr:
                addr = ":".join(addr[i : i + 2] for i in range(0, 12, 2))

            rings.append({"address": addr, "name": name, "source": "windows-paired"})

        dedup = {}
        for ring in rings:
            dedup[ring["address"]] = ring
        return list(dedup.values())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# RingScanner — scan and discover without any connection state
# ---------------------------------------------------------------------------


class RingScanner:
    """BLE scanning for Nuanic/Moodmetric rings.

    Holds only the config needed to scan (timeout, retry count, optional
    target address).  Returns raw discovery results; the caller (typically
    ``NuanicConnector``) decides what to do with them.
    """

    def __init__(
        self,
        timeout: float = 7.0,
        max_scan_attempts: int = 3,
        target_address: str | None = None,
        pair_on_connect: bool = True,
    ):
        self.timeout = timeout
        self.max_scan_attempts = max_scan_attempts
        self.target_address = target_address
        self.pair_on_connect = pair_on_connect

    async def find_device(self):
        """Scan for Nuanic ring.

        If target_address is set, search for that specific device.
        Returns the ``BLEDevice`` or ``None``.
        """
        search_label = (
            f"'{self.target_address}'" if self.target_address else "(any Nuanic)"
        )

        max_attempts = 1 if self.target_address else self.max_scan_attempts
        for attempt in range(1, max_attempts + 1):
            try:
                devices = await BleakScanner.discover(timeout=2.0)

                for device in devices:
                    if not device.name or (
                        "Nuanic" not in device.name and "Moodmetric" not in device.name
                    ):
                        continue

                    if self.target_address:
                        if device.address.lower() == self.target_address.lower():
                            return device
                    else:
                        return device

                if attempt < max_attempts:
                    await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt < max_attempts:
                    await asyncio.sleep(0.5)

        return None

    async def list_available_rings(
        self,
        include_device: bool = False,
        scan_timeout: float = 6.0,
        attempts: int = 3,
        retry_delay: float = 1.0,
        stop_if_found: bool = True,
        silent: bool = False,
    ) -> list[dict]:
        """Scan and return list of all available Nuanic rings."""
        if not silent:
            print(
                f"[SCAN] Discovering Nuanic rings (timeout: {scan_timeout}s, "
                f"attempts: {attempts})..."
            )

        merged: dict[str, dict] = {}
        try:
            for attempt in range(1, max(1, attempts) + 1):
                if attempts > 1 and not silent:
                    print(f"[SCAN] Attempt {attempt}/{attempts}...")

                current_timeout = scan_timeout
                if attempt == 1 and stop_if_found:
                    current_timeout = min(3.0, scan_timeout)

                devices_map = await BleakScanner.discover(
                    timeout=max(2.0, current_timeout), return_adv=True
                )

                for device, adv in devices_map.values():
                    name = device.name or ""
                    clean_name = _sanitize_name(name)
                    adv_uuids = [u.lower() for u in adv.service_uuids]

                    is_nuanic = (
                        "Nuanic" in name or NUANIC_SERVICE_UUID.lower() in adv_uuids
                    )
                    is_moodmetric = "Moodmetric" in name or any(
                        u.lower() in adv_uuids for u in MOODMETRIC_SERVICE_UUIDS
                    )

                    if is_nuanic or is_moodmetric:
                        addr = _sanitize_address(device.address or "")
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
                            entry["device"] = device  # type: ignore[assignment]
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

    async def list_available_rings_with_paired(
        self,
        scan_timeout: float = 6.0,
        attempts: int = 3,
        stop_if_found: bool = True,
        silent: bool = False,
    ) -> list[dict]:
        """Return discoverable rings plus Windows paired rings (if any)."""
        scanned = await self.list_available_rings(
            include_device=True,
            scan_timeout=scan_timeout,
            attempts=attempts,
            retry_delay=1.0,
            stop_if_found=stop_if_found,
            silent=silent,
        )
        paired = _get_windows_paired_rings()

        merged: dict[str, dict] = {}
        for ring in scanned:
            addr = _sanitize_address(ring["address"])
            name = _sanitize_name(ring["name"])
            merged[addr.upper()] = {
                "address": addr,
                "name": name,
                "device": ring.get("device"),
                "source": "scan",
            }

        for ring in paired:
            addr = _sanitize_address(ring["address"])
            name = _sanitize_name(ring["name"])
            key = addr.upper()
            if key not in merged:
                merged[key] = {
                    "address": addr,
                    "name": name,
                    "device": None,
                    "source": "windows-paired",
                }

        return list(merged.values())

    async def discover_all_matching_rings(
        self,
        include_device: bool = True,
        scan_timeout: float = 6.0,
        attempts: int = 3,
        retry_delay: float = 0.5,
        stop_if_found: bool = True,
        silent: bool = False,
    ) -> list[dict]:
        """Discover all visible Nuanic/Moodmetric rings."""
        scanned = await self.list_available_rings(
            include_device=include_device,
            scan_timeout=scan_timeout,
            attempts=attempts,
            retry_delay=retry_delay,
            stop_if_found=stop_if_found,
            silent=silent,
        )

        if platform.system() != "Windows":
            return scanned

        paired = _get_windows_paired_rings()
        merged = {entry["address"].upper(): entry for entry in scanned}
        for entry in paired:
            key = entry["address"].upper()
            if key not in merged:
                merged[key] = {
                    "address": entry["address"],
                    "name": entry.get("name") or "Nuanic",
                    "device": None,
                }

        if not merged:
            cached = _load_last_address()
            if cached:
                cached_clean = _sanitize_address(cached)
                if cached_clean:
                    merged[cached_clean.upper()] = {
                        "address": cached_clean,
                        "name": "Nuanic (cached)",
                        "device": None,
                    }

        return list(merged.values())

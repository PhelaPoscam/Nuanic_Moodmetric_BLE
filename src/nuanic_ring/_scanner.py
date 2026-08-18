"""BLE scanning and ring discovery (Compatibility Shim)."""

from nuanic_ring.core.scanner import (
    RingScanner,
    _get_windows_paired_rings,
    _load_last_address,
    _reset_bluetooth_radio,
    _sanitize_address,
    _sanitize_name,
    _save_last_address,
)

__all__ = [
    "RingScanner",
    "_sanitize_address",
    "_sanitize_name",
    "_save_last_address",
    "_load_last_address",
    "_reset_bluetooth_radio",
    "_get_windows_paired_rings",
]

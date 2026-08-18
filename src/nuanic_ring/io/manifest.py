"""Session provenance and data integrity manifest generator for Nuanic Ring."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def compute_sha256(file_path: Path) -> str:
    """Compute the SHA-256 hexadecimal digest for a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


@dataclass(slots=True)
class DeviceSessionStats:
    """Telemetry statistics for a single ring device during a session."""

    battery_start: Optional[int] = None
    battery_end: Optional[int] = None
    total_packets_received: int = 0
    dropped_rows: int = 0
    mean_observed_hz: float = 0.0
    d306_packets: int = 0
    imu_batches: int = 0
    live_eda_packets: int = 0
    finger_state_packets: int = 0


@dataclass(slots=True)
class SessionConfiguration:
    """Session operational configuration."""

    target_hz: float
    operational_mode: str
    filter_enabled: bool
    csv_layout: str
    participant_id: Optional[str] = None


@dataclass(slots=True)
class SessionManifest:
    """Provenance and cryptographic integrity manifest for a recording session."""

    sdk_version: str
    session_id: str
    start_time_iso: str
    end_time_iso: str
    duration_seconds: float
    configuration: Dict[str, Any]
    devices: Dict[str, Dict[str, Any]]
    checksums: Dict[str, str] = field(default_factory=dict)
    system_info: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert manifest to serializable dictionary."""
        return asdict(self)

    def write_to_file(self, target_path: Path) -> Path:
        """Write formatted JSON manifest to target path."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        return target_path


def get_sdk_version() -> str:
    """Retrieve SDK version string."""
    try:
        from importlib.metadata import version

        return version("nuanic-ring")
    except Exception:
        return "0.2.0"


def generate_session_manifest(
    session_dir: Path,
    session_id: str,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    configuration: Dict[str, Any],
    device_states: Dict[str, Any],
    sdk_version: Optional[str] = None,
) -> SessionManifest:
    """Generate session provenance manifest with SHA-256 checksums of all session files."""
    now = datetime.now(timezone.utc)
    st = (
        start_time.astimezone(timezone.utc)
        if start_time and start_time.tzinfo
        else (start_time.replace(tzinfo=timezone.utc) if start_time else now)
    )
    et = (
        end_time.astimezone(timezone.utc)
        if end_time and end_time.tzinfo
        else (end_time.replace(tzinfo=timezone.utc) if end_time else now)
    )

    duration = max(0.0, (et - st).total_seconds())

    # Build per-device metrics
    devices_summary: Dict[str, Dict[str, Any]] = {}
    for mac, state in device_states.items():
        d306 = getattr(state, "d306_count", 0)
        imu = getattr(state, "imu_batch_count", 0)
        eda = getattr(state, "live_eda_count", 0)
        state_count = getattr(state, "state_count", 0)
        total = d306 + imu + eda + state_count

        stats = DeviceSessionStats(
            battery_start=getattr(
                state, "battery_start", getattr(state, "battery", None)
            ),
            battery_end=getattr(state, "battery_end", getattr(state, "battery", None)),
            total_packets_received=total,
            dropped_rows=getattr(state, "dropped_rows", 0),
            mean_observed_hz=getattr(state, "d306_observed_hz", 0.0),
            d306_packets=d306,
            imu_batches=imu,
            live_eda_packets=eda,
            finger_state_packets=state_count,
        )
        devices_summary[mac] = asdict(stats)

    # Compute checksums for all CSVs and data files in session folder
    checksums: Dict[str, str] = {}
    if session_dir.exists():
        for file_path in sorted(session_dir.rglob("*.csv")):
            if file_path.is_file():
                rel_path = file_path.relative_to(session_dir).as_posix()
                checksums[rel_path] = compute_sha256(file_path)

    sys_info = {
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "processor": platform.processor() or "unknown",
    }

    manifest = SessionManifest(
        sdk_version=sdk_version or get_sdk_version(),
        session_id=session_id,
        start_time_iso=st.isoformat(),
        end_time_iso=et.isoformat(),
        duration_seconds=round(duration, 3),
        configuration=configuration,
        devices=devices_summary,
        checksums=checksums,
        system_info=sys_info,
    )

    manifest_path = session_dir / "session_manifest.json"
    manifest.write_to_file(manifest_path)
    return manifest

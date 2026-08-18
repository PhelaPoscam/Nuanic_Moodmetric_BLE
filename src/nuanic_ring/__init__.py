"""Nuanic Ring SDK: Bluetooth Low Energy data extraction and real-time monitoring."""

__version__ = "0.2.0"

from nuanic_ring.core.connector import NuanicConnector
from nuanic_ring.core.profiles import (
    MOODMETRIC_PROFILE,
    NUANIC_PROFILE,
    UNKNOWN_PROFILE,
    detect_ring_profile_from_service_uuids,
    notify_uuids_for_profile,
)
from nuanic_ring.dsp.signal_processing import SignalConditioner
from nuanic_ring.io.manifest import SessionManifest, generate_session_manifest
from nuanic_ring.io.schemas import (
    CombinedLogRow,
    ComputedLogRow,
    D306Packet,
    FingerStatePacket,
    ImuBatchPacket,
    ImuLogRow,
    LiveEdaPacket,
    NuanicExportLogRow,
    StreamLogRow,
)
from nuanic_ring.monitor import NuanicMonitor

# Re-export mode constants for convenience
MODE_STANDBY = NuanicConnector.MODE_STANDBY
MODE_RAW_EDA = NuanicConnector.MODE_RAW_EDA
MODE_LIVE = NuanicConnector.MODE_LIVE
MODE_RESEARCH = NuanicConnector.MODE_RESEARCH

__all__ = [
    "__version__",
    "NuanicConnector",
    "NuanicMonitor",
    "SignalConditioner",
    "SessionManifest",
    "generate_session_manifest",
    "D306Packet",
    "LiveEdaPacket",
    "ImuBatchPacket",
    "FingerStatePacket",
    "CombinedLogRow",
    "StreamLogRow",
    "ComputedLogRow",
    "ImuLogRow",
    "NuanicExportLogRow",
    "NUANIC_PROFILE",
    "MOODMETRIC_PROFILE",
    "UNKNOWN_PROFILE",
    "detect_ring_profile_from_service_uuids",
    "notify_uuids_for_profile",
    "MODE_STANDBY",
    "MODE_RAW_EDA",
    "MODE_LIVE",
    "MODE_RESEARCH",
]

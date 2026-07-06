"""Nuanic Ring integration module"""

from .connector import NuanicConnector
from .mm_compat import (
    MMFeatures,
    MMLikeScorer,
    decode_raw_resistance_packet,
    decode_streaming_packet,
)
from .monitor import NuanicMonitor
from .ring_profiles import (
    MOODMETRIC_PROFILE,
    NUANIC_PROFILE,
    UNKNOWN_PROFILE,
    detect_ring_profile_from_service_uuids,
    notify_uuids_for_profile,
)

# Re-export mode constants for convenience
MODE_STANDBY = NuanicConnector.MODE_STANDBY
MODE_RAW_EDA = NuanicConnector.MODE_RAW_EDA
MODE_LIVE = NuanicConnector.MODE_LIVE
MODE_RESEARCH = NuanicConnector.MODE_RESEARCH
# Backward-compat
MODE_ALGO = NuanicConnector.MODE_ALGO
MODE_EDA = NuanicConnector.MODE_EDA
MODE_EDA_VARIANT = NuanicConnector.MODE_EDA_VARIANT

__all__ = [
    "NuanicConnector",
    "NuanicMonitor",
    "MMFeatures",
    "MMLikeScorer",
    "decode_raw_resistance_packet",
    "decode_streaming_packet",
    "NUANIC_PROFILE",
    "MOODMETRIC_PROFILE",
    "UNKNOWN_PROFILE",
    "detect_ring_profile_from_service_uuids",
    "notify_uuids_for_profile",
    "MODE_STANDBY",
    "MODE_RAW_EDA",
    "MODE_LIVE",
    "MODE_RESEARCH",
    "MODE_ALGO",
    "MODE_EDA",
    "MODE_EDA_VARIANT",
]

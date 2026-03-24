"""Nuanic Ring integration module"""

from .connector import NuanicConnector
from .monitor import NuanicMonitor
from .logger import NuanicDataLogger
from .mm_compat import (
    MMFeatures,
    MMLikeScorer,
    decode_raw_resistance_packet,
    decode_streaming_packet,
)
from .ring_profiles import (
    MOODMETRIC_PROFILE,
    NUANIC_PROFILE,
    UNKNOWN_PROFILE,
    detect_ring_profile_from_service_uuids,
    notify_uuids_for_profile,
)
from .moodmetric_parser import (
    decode_moodmetric_payload,
    summarize_decoded_payload,
)

__all__ = [
    "NuanicConnector",
    "NuanicMonitor",
    "NuanicDataLogger",
    "MMFeatures",
    "MMLikeScorer",
    "decode_raw_resistance_packet",
    "decode_streaming_packet",
    "NUANIC_PROFILE",
    "MOODMETRIC_PROFILE",
    "UNKNOWN_PROFILE",
    "detect_ring_profile_from_service_uuids",
    "notify_uuids_for_profile",
    "decode_moodmetric_payload",
    "summarize_decoded_payload",
]

"""Packet decoders and callback helpers for Nuanic telemetry streams."""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional, Tuple

from nuanic_ring.io.schemas import (
    D306Packet,
    FingerStatePacket,
    ImuBatchPacket,
    LiveEdaPacket,
)


def parse_d306_packet(data: bytes) -> Optional[Dict[str, Any]]:
    """Decode 16-byte D306 packet into field dictionary."""
    packet = D306Packet.from_bytes(data)
    if not packet:
        return None
    return {
        "timestamp_ms": packet.timestamp_ms,
        "instant": packet.instant,
        "dne": packet.dne,
        "clock": packet.clock,
        "context": packet.context,
        "eda_value": packet.eda_value,
        "dne_stress_index": packet.dne_stress_index,
    }


def parse_468f_imu_batch(data: bytes) -> Optional[Dict[str, Any]]:
    """Decode IMU batch payload (468f2717...) into samples and intensity."""
    packet = ImuBatchPacket.from_bytes(data)
    if not packet:
        return None
    return {
        "clock": packet.clock,
        "context": packet.context,
        "samples": packet.samples,
        "motion_intensity": packet.motion_intensity,
        "first_x": packet.first_x,
        "first_y": packet.first_y,
        "first_z": packet.first_z,
    }


def parse_live_eda_packet(data: bytes) -> Optional[Dict[str, Any]]:
    """Decode 14-byte raw EDA packet into field dictionary."""
    packet = LiveEdaPacket.from_bytes(data)
    if not packet:
        return None
    res_kohm = packet.eda_raw_ohms / 1000.0
    cond_us = (1000000.0 / packet.eda_raw_ohms) if packet.eda_raw_ohms > 0 else 0.0
    return {
        "boot_count": packet.boot_count,
        "timestamp_ms": packet.timestamp_ms,
        "eda_ohm": packet.eda_raw_ohms,
        "resistance_kohm": round(res_kohm, 3),
        "conductance_us": round(cond_us, 3),
    }


def parse_finger_state_packet(data: bytes) -> FingerStatePacket:
    """Decode finger state indicator packet."""
    return FingerStatePacket.from_bytes(data)

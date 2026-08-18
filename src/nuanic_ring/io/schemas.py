"""Strongly typed schemas and dataclasses for Nuanic packets and CSV logging."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, List, Optional, Tuple

# ==============================================================================
# Packet Schemas (Binary GATT Payloads)
# ==============================================================================


@dataclass(slots=True)
class D306Packet:
    """Decoded 16-byte payload from LIVE_DNE characteristic (d306262b...)."""

    timestamp_ms: int
    instant: int
    dne: int

    @property
    def clock(self) -> int:
        return self.timestamp_ms & 0xFFFFFFFF

    @property
    def context(self) -> int:
        return self.instant

    @property
    def eda_value(self) -> int:
        return self.instant

    @property
    def dne_stress_index(self) -> int:
        return self.dne

    @classmethod
    def from_bytes(cls, data: bytes) -> Optional[D306Packet]:
        if len(data) != 16:
            return None
        timestamp_ms, instant, dne = struct.unpack("<Qii", data)
        return cls(timestamp_ms=timestamp_ms, instant=instant, dne=dne)


@dataclass(slots=True)
class LiveEdaPacket:
    """Decoded 14-byte payload from LIVE_EDA characteristic (42dcb71b...)."""

    boot_count: int
    timestamp_ms: int
    eda_raw_ohms: int

    @classmethod
    def from_bytes(cls, data: bytes) -> Optional[LiveEdaPacket]:
        if len(data) != 14:
            return None
        boot_count, timestamp_ms, eda_raw_ohms = struct.unpack("<HQI", data)
        return cls(
            boot_count=boot_count,
            timestamp_ms=timestamp_ms,
            eda_raw_ohms=eda_raw_ohms,
        )


@dataclass(slots=True)
class ImuBatchPacket:
    """Decoded accelerometer batch payload from IMU_BATCH characteristic (468f2717...)."""

    clock: int
    context: int
    samples: List[Tuple[int, int, int]]
    motion_intensity: float
    first_x: int
    first_y: int
    first_z: int

    @classmethod
    def from_bytes(cls, data: bytes) -> Optional[ImuBatchPacket]:
        if len(data) < 8:
            return None

        clock, context = struct.unpack("<II", data[:8])
        sample_bytes = data[8:]
        sample_count = len(sample_bytes) // 6

        samples: List[Tuple[int, int, int]] = []
        variances: List[float] = []

        for i in range(sample_count):
            chunk = sample_bytes[i * 6 : (i + 1) * 6]
            if len(chunk) == 6:
                x, y, z = struct.unpack("<hhh", chunk)
                samples.append((x, y, z))
                variances.append(float(x * x + y * y + z * z))

        if not samples:
            return None

        motion_intensity = sum(variances) / len(variances) if variances else 0.0
        first_x, first_y, first_z = samples[0]

        return cls(
            clock=clock,
            context=context,
            samples=samples,
            motion_intensity=motion_intensity,
            first_x=first_x,
            first_y=first_y,
            first_z=first_z,
        )


@dataclass(slots=True)
class FingerStatePacket:
    """Decoded finger contact state from STATE characteristic (3c180fcc...)."""

    state_code: Optional[int]
    payload_hex: str

    @classmethod
    def from_bytes(cls, data: bytes) -> FingerStatePacket:
        code = data[0] if len(data) >= 1 else None
        return cls(state_code=code, payload_hex=data.hex())


# ==============================================================================
# CSV Row Schemas
# ==============================================================================


@dataclass(slots=True)
class CombinedLogRow:
    """Schema for unified combined session log (all telemetry and computed signals)."""

    timestamp: str
    elapsed_ms: int
    device_mac: str
    connection_state: str
    data_type: str
    EDA_Raw_Value: Any = ""
    Stress_Index: Any = ""
    D306_Clock: Any = ""
    D306_Context: Any = ""
    State_Code: Any = ""
    payload_hex: str = ""
    full_packet_hex: str = ""
    decoded_fields: str = ""
    D306_Observed_Hz: str = ""
    IMU_Observed_Hz: str = ""
    Rate_Target_Hz: str = ""
    Rate_Control_Status: str = ""
    Equalize_Mode: str = ""
    Equalize_WouldDrop: str = ""

    def to_csv_row(self) -> List[Any]:
        return [getattr(self, f.name) for f in fields(self)]

    @classmethod
    def header(cls) -> List[str]:
        return [f.name for f in fields(cls)]


@dataclass(slots=True)
class StreamLogRow:
    """Schema for _streamed.csv log."""

    timestamp: str
    elapsed_ms: int
    device_mac: str
    connection_state: str
    data_type: str
    D306_Clock: Any = ""
    D306_Context: Any = ""
    EDA_Raw_Value: Any = ""
    Stress_Index: Any = ""
    State_Code: Any = ""
    payload_hex: str = ""
    full_packet_hex: str = ""
    decoded_fields: str = ""
    marker_label: str = ""
    marker_source: str = ""

    def to_csv_row(self) -> List[Any]:
        return [getattr(self, f.name) for f in fields(self)]

    @classmethod
    def header(cls) -> List[str]:
        return [f.name for f in fields(cls)]


@dataclass(slots=True)
class ComputedLogRow:
    """Schema for _computed.csv log."""

    timestamp: str
    elapsed_ms: int
    device_mac: str
    connection_state: str
    data_type: str
    Source_D306_Clock: Any = ""
    Source_D306_Context: Any = ""
    Skin_Resistance_kOhm: Any = ""
    Skin_Conductance_uS: Any = ""
    MM_Filtered_uS: Any = ""
    SCR_Frequency_Per_Min: Any = ""
    SCR_Amplitude: Any = ""
    MM_Arousal_Score: Any = ""
    MM_Calibrated: Any = ""
    D306_Observed_Hz: str = ""
    IMU_Observed_Hz: str = ""
    Rate_Target_Hz: str = ""
    Rate_Control_Status: str = ""
    Equalize_Mode: str = ""
    Equalize_WouldDrop: str = ""
    marker_label: str = ""
    marker_source: str = ""

    def to_csv_row(self) -> List[Any]:
        return [getattr(self, f.name) for f in fields(self)]

    @classmethod
    def header(cls) -> List[str]:
        return [f.name for f in fields(cls)]


@dataclass(slots=True)
class ImuLogRow:
    """Schema for _imu.csv log."""

    timestamp: str
    elapsed_ms: int
    clock: int
    context: int
    motion_intensity: str
    x: int
    y: int
    z: int
    marker: str = ""

    def to_csv_row(self) -> List[Any]:
        return [getattr(self, f.name) for f in fields(self)]

    @classmethod
    def header(cls) -> List[str]:
        return [f.name for f in fields(cls)]


@dataclass(slots=True)
class NuanicExportLogRow:
    """Schema for Nuanic Cloud compatible CSV export."""

    address: str
    time_unix: str
    time: str
    dne: Any
    srl: Any
    srrn: Any
    eda: Any

    def to_csv_row(self) -> List[Any]:
        return [getattr(self, f.name) for f in fields(self)]

    @classmethod
    def header(cls) -> List[str]:
        return [f.name for f in fields(cls)]

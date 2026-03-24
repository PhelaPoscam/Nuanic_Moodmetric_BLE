"""First-pass parser helpers for Moodmetric notify payloads."""

from __future__ import annotations

from typing import Any

MM_UUID_A095 = "a0956420-9bd2-11e4-bd06-0800200c9a66"
MM_UUID_90BD = "90bd4fd0-4309-11e4-916c-0800200c9a66"
MM_UUID_F1B4 = "f1b41cde-dbf5-4acf-8679-ecb8b4dca6ff"
MM_UUID_5D7A = "5d7a90a0-ab7e-11e4-bcd8-0800200c9a66"
MM_UUID_C486 = "c48650d0-a2d8-11e4-bcd8-0800200c9a66"


def _u16le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little", signed=False)


def decode_moodmetric_payload(uuid: str, payload: bytes) -> dict[str, Any]:
    """Decode known Moodmetric UUID payloads into numeric fields.

    Returns a dict with at least:
    - uuid
    - len
    - frame_type
    """
    u = uuid.lower()
    out: dict[str, Any] = {
        "uuid": u,
        "len": len(payload),
        "frame_type": "unknown",
    }

    if u == MM_UUID_A095 and len(payload) == 7:
        counter = _u16le(payload, 0)
        state_like = _u16le(payload, 2)
        stress_like = payload[4]
        signal_lo = payload[5]
        signal_hi = payload[6]
        signal_u16 = int.from_bytes(payload[5:7], "little", signed=False)

        out.update(
            {
                "frame_type": "a095_compact",
                "counter_u16": counter,
                "state_like_u16": state_like,
                "stress_like_u8": stress_like,
                "signal_lo_u8": signal_lo,
                "signal_hi_u8": signal_hi,
                "signal_u16": signal_u16,
            }
        )
        return out

    if u == MM_UUID_90BD and len(payload) == 12:
        words = [_u16le(payload, i) for i in range(0, 12, 2)]
        stress_like = payload[6]
        signal_lo = payload[8]
        signal_hi = payload[10]
        signal_u16 = int.from_bytes(bytes([signal_lo, signal_hi]), "little")

        out.update(
            {
                "frame_type": "90bd_expanded",
                "w0_u16": words[0],
                "w1_u16": words[1],
                "w2_u16": words[2],
                "w3_u16": words[3],
                "w4_u16": words[4],
                "w5_u16": words[5],
                "stress_like_u8": stress_like,
                "signal_lo_u8": signal_lo,
                "signal_hi_u8": signal_hi,
                "signal_u16": signal_u16,
            }
        )
        return out

    if u == MM_UUID_F1B4 and len(payload) == 2:
        out.update(
            {
                "frame_type": "f1b4_raw_u16",
                "adc_like_u16": _u16le(payload, 0),
            }
        )
        return out

    if u == MM_UUID_5D7A and len(payload) == 11:
        # 5 little-endian words + trailing status byte.
        out.update(
            {
                "frame_type": "5d7a_eventish",
                "w0_u16": _u16le(payload, 0),
                "w1_u16": _u16le(payload, 2),
                "w2_u16": _u16le(payload, 4),
                "w3_u16": _u16le(payload, 6),
                "w4_u16": _u16le(payload, 8),
                "tail_u8": payload[10],
            }
        )
        return out

    if u == MM_UUID_C486:
        out["frame_type"] = "c486_unknown"
        return out

    return out


def summarize_decoded_payload(decoded: dict[str, Any]) -> str:
    """Compact one-line summary for console logs."""
    frame_type = decoded.get("frame_type", "unknown")

    if frame_type == "a095_compact":
        return (
            f"type=a095 ctr={decoded.get('counter_u16')} state={decoded.get('state_like_u16')} "
            f"stress?={decoded.get('stress_like_u8')} sig16={decoded.get('signal_u16')}"
        )

    if frame_type == "90bd_expanded":
        return (
            f"type=90bd w0={decoded.get('w0_u16')} w1={decoded.get('w1_u16')} "
            f"stress?={decoded.get('stress_like_u8')} sig16={decoded.get('signal_u16')}"
        )

    if frame_type == "f1b4_raw_u16":
        return f"type=f1b4 adc?={decoded.get('adc_like_u16')}"

    if frame_type == "5d7a_eventish":
        return (
            f"type=5d7a w0={decoded.get('w0_u16')} w1={decoded.get('w1_u16')} "
            f"tail={decoded.get('tail_u8')}"
        )

    return f"type={frame_type}"

# TODO — Reverse-engineering unknowns

## Flash storage trigger

The ring has onboard flash memory but we haven't found how to start recording.
Neither `MODE_LIVE` (0x02) nor `MODE_RESEARCH` (0x03) writes to the buffer.

**Leading candidate:** `WRITE_1` (`2175c13f...`) with a structured multi-byte payload.
A 1-byte write was a no-op. Likely expects:
- 8-byte Unix timestamp (time sync + session start marker)
- Or a magic key / auth challenge struct

**Probe:** Try `struct.pack("<I", epoch)` and `struct.pack("<Q", epoch)` writes to `WRITE_1`,
then check `7c3b82e7` for data. Also test `CONFIG_3` values above 0x03 (0x04, 0xFF).

## Buffer download protocol

Once recording is triggered, we need to pull data from `7c3b82e7`. Currently it
returns 0 bytes. The official app likely:
1. Writes a "request dump" command to `WRITE_1`
2. Reads `7c3b82e7` in chunks (notifications? repeated reads?)
3. Writes an "ack/clear" command when done

## 42dc packet — confirming sample rate

We've verified CONFIG_1 controls d306 rate universally. 42dc should follow the same
register, but hasn't been explicitly tested. Set CONFIG_1 to 3 Hz or 16 Hz while in
Mode 0x01 and confirm 42dc packet rate changes accordingly.

## Known / verified ✅

- All 4 modes mapped (Standby / Raw EDA / Live / Research)
- 60-second calibration law — every mode transition
- d306 16-byte structure: Clock + Ctx + Raw EDA + DNE (uint32 LE × 4)
- 42dc 14-byte structure: Header + Clock + Ctx + Raw EDA (uint16) + Format tag
- 0x02 vs 0x03 = DNE filter window (short vs long)
- CONFIG_1 = sample rate 3–16 Hz (universal)
- CONFIG_2 = free-running millisecond clock (18 bytes)
- No mode writes to flash storage

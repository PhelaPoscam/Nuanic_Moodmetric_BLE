# Nuanic Ring Reverse-Engineering Report

## Summary
After reverse-engineering of the Nuanic ring BLE communication protocol, we have identified the actual data being transmitted and discovered that the ring has **limited EDA capabilities**.

## Update (2026-03-16): Two Ring Types Detected in Practice

Recent diagnostics confirmed two distinct BLE profiles can appear in local workflows:

### 1. Nuanic profile
- Proprietary service: `5491faaf-b0c2-4167-8f3d-bc6b31db69e7`
- Includes project-specific characteristics used by monitor/logger logic (for example `7c3b82e7...`, `d306262b...`, `468f2717...`).

### 2. Moodmetric profile
- Different custom services observed:
  - `dd499b70-e4cd-4988-a923-a7aab7283f8e`
  - `aed4978e-9c7a-11e3-8d05-425861b86ab6`
  - `0000e001-0000-1000-8000-00805f9b34fb`
- Does **not** expose Nuanic proprietary service `5491faaf...`.
- Nuanic-only buffer path (`7c3b82e7...`) is not compatible and should be skipped.

Practical command for ring-type validation:
```bash
python scripts/discover_ring_services.py --no-profile --buffer-poll 0
```

## Update (2026-03-16): Moodmetric Notify Stream Breakdown

A focused Moodmetric monitor session produced sustained live notifications and enabled first-pass field mapping.

### Observed UUID behavior (session-specific)
- Active/high traffic:
  - `a0956420-9bd2-11e4-bd06-0800200c9a66` (7 bytes)
  - `90bd4fd0-4309-11e4-916c-0800200c9a66` (12 bytes)
  - `f1b41cde-dbf5-4acf-8679-ecb8b4dca6ff` (2 bytes)
- Occasional/event-like:
  - `5d7a90a0-ab7e-11e4-bcd8-0800200c9a66` (11 bytes observed)
- Silent in this capture:
  - `c48650d0-a2d8-11e4-bcd8-0800200c9a66`

### Structural hypothesis
- `90bd...` and `a095...` appear to encode overlapping data (expanded vs condensed frame view).
- Candidate `a095...` layout:
  - bytes 0-1: rolling counter/clock
  - bytes 2-3: state/quality-like scalar
  - byte 4: stress-like index candidate
  - bytes 5-6: raw signal / EDA-like candidate
- `f1b4...` appears to be a compact high-rate raw ADC-like reading.

Example paired frame from capture:
- `90bd`: `400000850092920074004400`
- `a095`: `032d4000927444`

Quantitative validation from captured CSV (`nuanic_2026-03-16_16-02-21.csv`):
- Packet counts: `f1b4=63`, `90bd=62`, `a095=61`, `5d7a=1`, `c486=0`
- Effective rates for main channels are ~`3 Hz` each over the capture window.
- Byte mapping check between aligned `a095` and `90bd` packets matched in `61/61` pairs using:
  - `a095[2:4] == 90bd[0:2]`
  - `a095[4] == 90bd[6]`
  - `a095[5] == 90bd[8]`
  - `a095[6] == 90bd[10]`

### Confidence level
- Medium for frame overlap pattern.
- Low-to-medium for semantic labels (stress/EDA/raw ADC) until controlled validation is completed.

## Findings

## Update (2026-03-16): Revised Live Stream Interpretation

This update reflects a direct multi-notify capture using the diagnostics mode that subscribes to all four proprietary notify characteristics simultaneously.

### 1. State / On-Finger Indicator

- **UUID:** `3c180fcc-bfec-4b7c-8e52-1a37f123e449`
- **Payload:** 1 byte
- **Observed values:** `01`, `02`, `03`

Current interpretation:
- `01` = idle/off-finger (or low-power polling state)
- `02` = active/on-finger state
- `03` = transient/poll state seen around idle transitions

In the captured session, transition to `02` coincided with immediate start of high-rate streams, and transition back to `01` coincided with stream stop.

### 2. Real-Time Sensor + Quality Stream

- **UUID:** `d306262b-c8c9-4c4b-9050-3a41dea706e5`
- **Payload:** 16 bytes
- **Frequency:** approximately 22-25 Hz in the latest run

4x uint32 (little-endian) working layout hypothesis:
- **Bytes 0-3:** monotonic packet clock/counter
- **Bytes 4-7:** static context/session field (commonly `9C 01 00 00` in this run)
- **Bytes 8-11:** dynamic physiological signal (EDA/stress candidate)
- **Bytes 12-15:** contact/quality-like metric (observed at `0x64` then drifting downward)

### 3. Bulk Motion/IMU Stream

- **UUID:** `468f2717-6a7d-46f9-9eb7-f92aab208bae`
- **Payload:** 92 bytes
- **Frequency:** approximately 1 Hz

Current interpretation:
- First 8 bytes carry timestamp/counter context.
- Remaining 84 bytes are batched samples (likely one-second motion/IMU waveform payload).

### 4. Silent/Event Stream

- **UUID:** `42dcb71b-1817-43bd-8ea3-7272780a1c9f`
- **Observed behavior in this run:** no notifications

Current interpretation:
- Likely asynchronous/event-oriented channel (for sync, errors, battery, or deferred transfers).

### Confidence and Scope

- These findings are based on live payload behavior from the latest capture and align with stream timing relationships.
- This should be treated as the current best-fit model pending additional controlled sessions (off-finger/on-finger transitions, motion-only segments, and stress provocation segments).

### ✅ Active Data Streams

#### 1. **Stress Characteristic** (`468f2717-6a7d-46f9-9eb7-f92aab208bae`)
- **Frequency:** ~1 Hz
- **Packet Size:** 92 bytes fixed
- **Structure:**
  - Byte 14: Stress percentage (0-255 mapped to %)
  - Bytes 15-92: Waveform payload (77 uint8 values representing physiological signal)
  - Rest: Metadata/timestamp data
- **Interpretation:** This is maybe the EDA

#### 2. **IMU Characteristic** (`d306262b-c8c9-4c4b-9050-3a41dea706e5`)
- **Frequency:** 15.94 Hz
- **Packet Size:** 16 bytes
- **Data:** Accelerometer (3-axis) + timestamp
- **Use Case:** Motion/activity detection

### ❌ Broken/Non-Functional Streams

#### 3. **Mystery Notify Characteristic** (`42dcb71b-1817-43bd-8ea3-7272780a1c9f`)
- **Status:** ❌ Not sending data
- **Packets Observed:** 0 in 10+ second listening windows
- **Conclusion:** Unknown purpose, not actively used

#### 4. **EDA Buffer Characteristic** (`7c3b82e7-22b7-4cb6-8458-ba325edf6ede`)
- **Status:** ⚠️ One-time snapshot, now empty
- **Initial State:** 484 bytes containing structured records with float32 values
- **Current State:** 0 bytes (pre-recorded data cleared after access)
- **Conclusion:** Historical data buffer, not useful for real-time visualization

### 📝 Configuration Characteristics (Write-enabled)

The following characteristics accept writes and appear to be configuration registers:

1. **`516b0fb6-d861-4619-9dd0-0105e8b85128`** - Echo register (stores written value)
2. **`dc9c31a7-fbd3-467a-8777-10900c423d3b`** - Timestamp register (always returns current time)
3. **`3cce21a7-e602-4e02-8c52-1e0366c1c846`** - Config register (echoes writes)
4. **`2175c13f-60e4-4de5-80af-0d06f1b54880`** - Write-only register (purpose unknown)

**Testing Attempts:**
- Wrote 8 different command patterns (0x01, 0x02, 0xFF, etc.)
- **Result:** All writes accepted, but no change in data streams or mystery notify activation
- **Conclusion:** Write commands may not decode properly or registers are for diagnostic/logging only

## Recommendations

- **Stress Percentage** (byte 14 from stress characteristic)
  - Aggregated stress level (0-100%)
  - Updates at 1 Hz
  - Use as overlay on waveform

- **IMU Data** (acceleration)
  - Useful for motion/activity context
  - 16 Hz sample rate is good for motion capture

## Current Waveform Viewer Status

## Conclusion

The Nuanic ring **does transmit physiological data** via the "Stress" characteristic, but it's transmitted as a 77-byte waveform payload at 1 Hz rather than high-frequency raw EDA samples. This is sufficient for stress level monitoring and visualization, but not for detailed analysis.

The ring **does not have** a functioning separate raw EDA stream. All physiological data is in the stress characteristic waveform payload.

---

**Last Updated:** March 13, 2026  
**Reverse-Engineering Method:** BLE characteristic scanning, packet structure analysis, write command testing  
**Certainty Level:** High (validated with packet inspection and multiple connection tests)

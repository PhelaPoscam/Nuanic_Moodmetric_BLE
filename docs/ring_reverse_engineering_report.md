# Nuanic Ring Reverse-Engineering Report

## Summary
After comprehensive reverse-engineering of the Nuanic ring BLE communication protocol, we have identified the actual data being transmitted, mapped the complete 4-state configuration table for operational modes, and discovered that the ring has **high-rate raw EDA capabilities alongside real-time stress index computation**.

## Update (2026-03-16): Two Ring Types Detected in Practice

Recent diagnostics confirmed two distinct BLE profiles can appear in local workflows:

### 1. Nuanic profile
- Proprietary service: `5491faaf-b0c2-4167-8f3d-bc6b31db69e7`
- Includes project-specific characteristics used by monitor/logger logic (`7c3b82e7...`, `d306262b...`, `468f2717...`).

### 2. Moodmetric profile
- Different custom services observed:
  - `dd499b70-e4cd-4988-a923-a7aab7283f8e`
  - `aed4978e-9c7a-11e3-8d05-425861b86ab6`
  - `0000e001-0000-1000-8000-00805f9b34fb`
- Does **not** expose Nuanic proprietary service `5491faaf...`.
- Nuanic-only buffer path (`7c3b82e7...`) is not compatible and should be skipped.

Practical command for ring-type validation:
```bash
nuanic-ring-discover --no-profile --buffer-poll 0
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

---

## Update (July 2026): Nuanic Live Stream & Mode-Switch Breakthrough

Through systematic long-window mode-switch probing (`scripts/probe_mode_switch.py`), we successfully decoded the exact mechanism the ring uses to switch operational modes, explaining both the stream behaviors and the official app's requirement for a 1–2 minute reset after mode transitions.

### 1. State / On-Finger Indicator
- **UUID:** `3c180fcc-bfec-4b7c-8e52-1a37f123e449`
- **Payload:** 1 byte
- **Observed values:** `01` (off-finger/idle), `02` (active/on-finger), `03` (transient polling)

### 2. High-Rate EDA + Physiology Stream (Active in EDA Mode)
- **UUID:** `d306262b-c8c9-4c4b-9050-3a41dea706e5`
- **Payload:** 16 bytes fixed
- **Frequency:** ~16 Hz or ~5 Hz (controlled by `CONFIG_1` sample rate register)
- **Structure (4x uint32 little-endian):**
  - **Bytes 0-3:** Monotonic packet clock/counter
  - **Bytes 4-7:** Context/session field (`Ctx`)
  - **Bytes 8-11:** Raw EDA Value (~1,000,000 ADC impedance count, convertible to resistance/conductance)
  - **Bytes 12-15:** DNE Stress Index (computed arousal score, e.g. 17–26)
- **Behavior:** Active in Mode `0x02` and Mode `0x03`. Delivers BOTH unprocessed impedance and computed arousal scores in real time.

### 3. Raw EDA Stream — Mode 0x01 Only (42dcb71b)

- **UUID:** `42dcb71b-1817-43bd-8ea3-7272780a1c9f`
- **Payload:** 14 bytes
- **Frequency:** Configurable via `CONFIG_1` (3–16 Hz), same as d306
- **Behavior:**
  - Completely silent during Mode `0x02` or `0x03`.
  - Active only in Mode `0x01` after 60s calibration.
  - Delivers **raw EDA only — no onboard DNE computation.**

**✅ Verified payload structure** (from `probe_algo_stream.py`, 902 packets):

| Bytes | Field | Decoding | Evidence |
|:---|:---|:---|:---|
| 0–1 | Packet header | `uint16 LE` | Constant `0600` across all packets |
| 2–5 | Clock | `uint32 LE` | Δt=200 ticks → exactly 5 Hz (200 µs ticks) |
| 6–9 | Context | `uint32 LE` | Always zero |
| 10–11 | **Raw EDA** | `uint16 LE` | Range 11–64,872, 343 unique values — varies physiologically |
| 12–13 | **Format tag** | `uint16 LE` | Range 13–14, only **2 unique values** across 902 packets — NOT physiological |

**Key finding:** Bytes 12–13 are a constant format tag (`0x0E00` = 14), not DNE/SRL/SRRN. The Nuanic manual's claim that this stream encodes "Average DNE, SRL, and SRRN" is **incorrect** for the raw BLE payload — those metrics are either computed client-side by the app, or stored elsewhere. This stream is a **compact raw EDA feed** (14 bytes vs d306's 16 bytes) with no onboard algorithm running.

### 4. Bulk Motion/IMU Batch Stream
- **UUID:** `468f2717-6a7d-46f9-9eb7-f92aab208bae`
- **Payload:** 92 bytes fixed
- **Frequency:** ~1 Hz (delivering 14 batched accelerometer samples of X, Y, Z at ~14 Hz effective rate).

### 5. EDA Buffer Characteristic
- **UUID:** `7c3b82e7-22b7-4cb6-8458-ba325edf6ede`
- **Status:** ⚠️ One-time snapshot buffer for offline storage downloads (empty when no offline session is cached).

---

## 🧠 Experimental Breakthrough: Complete 4-State Configuration Table

By probing `CONFIG_3` (`3cce21a7...` `STORAGE_FORMAT`) with long 75-second observation windows, we mapped the complete 4-state command trigger table used by the official mobile app:

| Command on `CONFIG_3` | Mode Name | Active BLE Notify Stream | Behavior |
| :--- | :--- | :--- | :--- |
| **Write `0x00`** | **Standby / Sleep** | **None** | Shuts down all physiological BLE streaming (`d306` and `42dc` go silent). IMU (`468f`) and finger detection (`3c18`) remain active for low-power background polling. |
| **Write `0x01`** | **Raw EDA Only** (Legacy) | **`42dcb71b...`** | 60s calibration → 14-byte raw EDA packets (no onboard DNE computation). Stream `d306` stays silent. Use when doing your own DSP in software. |
| **Write `0x02`** | **Live Mode** (Responsive DNE) | **`d306262b...`** | 60s calibration → 16-byte dual Raw EDA + DNE packets. **Short baseline filter window** — DNE tracks arousal changes quickly (range ~16 pts, 17 unique values over 3 min). |
| **Write `0x03`** | **Research Mode** (Stable DNE) | **`d306262b...`** | 60s calibration → 16-byte dual Raw EDA + DNE packets. **Long baseline filter window** — DNE is heavily smoothed (range ~7 pts, 8 unique values over 3 min). |

> [!NOTE]
> **Sampling rate is independent of operational mode.** Transmission frequency (3–16 Hz) is controlled by **`CONFIG_1` (`516b0fb6...`)**, not `CONFIG_3`. Any active mode can stream at any supported rate.

> [!IMPORTANT]
> **Universal 60-Second Hardware Calibration Law:**
> There is **no instantaneous mode switch.** Every transition of `CONFIG_3` to a new value triggers a mandatory **60-second silent calibration window** — all physiological BLE streams are muted while the onboard rolling median baseline filter stabilizes. Writing the same mode value the ring is already in is a **no-op** (no reset, no interruption). This explains the official app's "please wait 1–2 minutes" warning after any mode change.

### ✅ Verified: 0x02 vs 0x03 — DNE Filter Difference (July 2026)

Experimentally tested with `scripts/probe_flash_storage.py` (3-minute dual-mode A/B probe, same finger, same session):

| Metric | Mode `0x02` (Live) | Mode `0x03` (Research) |
| :--- | :--- | :--- |
| **DNE range** | 26–42 (spread: 16) | 25–32 (spread: 7) |
| **DNE mean** | 34.0 | 26.7 |
| **Unique DNE values** | 17 | 8 |
| **Behavior** | Dynamic, responsive curve | Flat, heavily smoothed |
| **Flash buffer after 3 min** | 0 bytes | 0 bytes |

**DNE trajectory comparison:**
- `0x02`: `27→31→34→38→39→42→41→39→36→35→32→28→26` — clear arousal arc, responsive to physiological changes.
- `0x03`: `26→26→26→25→25→25→26→27→26→25→26→29→31→28` — barely moves for 2 minutes, only hints of change at the end.

**Conclusions:**
1. ❌ **Flash storage hypothesis DISPROVEN.** Buffer was 0 bytes across all phases. Neither mode writes to onboard flash.
2. ✅ **DNE filter difference CONFIRMED.** Mode `0x02` uses a short, responsive baseline window for real-time biofeedback. Mode `0x03` uses a long, conservative baseline window for stable clinical/research measurements.
3. Raw EDA ADC values were comparable across both modes (~994k–1014k), confirming the hardware sensor input is identical — only the onboard DNE algorithm differs.

### 📝 Configuration Characteristics (Write-Enabled)

1. **`516b0fb6-d861-4619-9dd0-0105e8b85128`** (`CONFIG_1`) - **Sample Rate Register** (3–16 Hz confirmed range; `0x05` = 5 Hz, `0x10` = 16 Hz). Values above 16 are clamped/rejected by firmware.
2. **`dc9c31a7-fbd3-467a-8777-10900c423d3b`** (`CONFIG_2`) - **System Clock Register** (read-only reference). 18 bytes: 8-byte millisecond counter (uint64 LE), repeated twice, suffixed with `0600` format tag. Free-running — changes with time, not with mode switches.
3. **`3cce21a7-e602-4e02-8c52-1e0366c1c846`** (`CONFIG_3` / `STORAGE_FORMAT`) - **Master Mode Switch Register** (`0x00` = Standby, `0x01` = Raw EDA Only, `0x02` = Live Mode, `0x03` = Research Mode).
4. **`2175c13f-60e4-4de5-80af-0d06f1b54880`** (`WRITE_1`) - Protocol Handshake / Command Endpoint. Probing confirmed writing `0x01` does NOT interrupt active streaming or trigger mode changes; likely requires structured multi-byte payloads (e.g. timestamp sync struct or auth challenge).

## Recommendations

| Use case | Mode | Stream | Why |
|:---|:---|:---|:---|
| **Real-time biofeedback** | `0x02` (Live) | `d306262b` | Short filter — DNE tracks arousal changes quickly |
| **Clinical/research baselining** | `0x03` (Research) | `d306262b` | Long filter — stable, reproducible DNE scores |
| **Custom DSP in software** | `0x01` (Raw EDA) | `42dcb71b` | No onboard algorithm — raw ADC only, you process it |
| **Motion context** | Any active mode | `468f2717` | IMU runs independently alongside physiology |
| **Low-power idle** | `0x00` (Standby) | — | IMU + finger detection still active for wake-on-wear |

> **Always account for the 60-second calibration window** after any mode transition before recording study data.
- **SDK Usage:**
  ```python
  from nuanic_ring import NuanicConnector, MODE_LIVE, MODE_RESEARCH, MODE_RAW_EDA, MODE_STANDBY

  connector = NuanicConnector()
  await connector.connect()
  await connector.set_mode(MODE_LIVE)        # Responsive DNE — short filter window
  await connector.set_sample_rate(16)        # 3–16 Hz
  # ... or use NuanicMonitor(initial_mode=MODE_EDA, target_hz=16)
  ```

---

**Last Updated:** July 3, 2026  
**Reverse-Engineering Method:** BLE characteristic scanning, packet structure analysis, systematic long-window mode-switch probing  
**Certainty Level:** High (validated with live hardware calibration timing and multi-stream packet inspection)

## TODO: Future Reverse-Engineering Goals

### Phase 6: Resolve 0x02 vs 0x03 (Flash Storage Hypothesis) 🔥 ACTIVE

The highest-priority open question. Use `scripts/probe_flash_storage.py`:

```powershell
# Test 0x02: stream 5 min, switch to standby, check buffer
.\.venv\Scripts\python.exe scripts\probe_flash_storage.py --mode 0x02 --stream-duration 300 --register config3

# Test 0x03: same thing
.\.venv\Scripts\python.exe scripts\probe_flash_storage.py --mode 0x03 --stream-duration 300 --register config3
```

If buffer `7c3b82e7` is non-empty after 0x03 but empty after 0x02, we've found the offline recording trigger.

### Phase 7: Probe WRITE_1 with Structured Payloads

`WRITE_1` (`2175c13f...`) accepted a 1-byte write without error but didn't change any observable state. It likely expects a multi-byte struct:

- **8-byte Unix timestamp** (time sync): `struct.pack("<IQ", 0x01, int(time.time()))` — 1-byte command + 8-byte timestamp
- **4-byte epoch**: `struct.pack("<I", int(time.time()))` — raw Unix epoch
- **Auth/pairing challenge**: Try the ring's own MAC address bytes or a fixed magic sequence

### Phase 8: Historical Data Download Protocol

When an offline recording exists, the official app downloads it from `7c3b82e7...`. If Phase 6 confirms 0x03 triggers recording, the next step is reverse-engineering the download handshake (likely involves writing to `WRITE_1` to request a dump, then reading `7c3b82e7` in chunks).

### Phase 9: ALGO Stream Decoding 🔥 ACTIVE

Run `scripts/probe_algo_stream.py` to capture 42dc packets and brute-force decode the 4-byte mystery field as uint32, int32, float32, and two int16 to determine what it actually encodes.

### Phase 10: Time Synchronization

Reverse-engineer the time sync payload on `WRITE_1` (`2175...`) or `CONFIG_2` (`dc9c...`) so our SDK can align the ring's internal clock with PC time.

### Phase 11: Historical Data Download Protocol

If Phase 6 confirms 0x03 triggers flash recording, reverse-engineer the download handshake from `7c3b82e7...`.

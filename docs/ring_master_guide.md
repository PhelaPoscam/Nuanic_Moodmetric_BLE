# Nuanic Ring Master Guide

Operational guide for running the SDK in daily development.

For packet-level reverse-engineering narrative and historical findings, see `ring_reverse_engineering_report.md`.

## Scope

This guide covers:
- Environment setup
- Primary commands
- Python API usage
- Practical troubleshooting

This guide intentionally avoids deep packet forensics to prevent duplication with the reverse-engineering report.

## Environment Setup

```bash
python -m venv .venv
# Windows
.\.venv\Scripts\Activate.ps1
# Linux/Mac
source .venv/bin/activate

pip install -e ".[dev]"
```

## Primary Commands

### Monitor

```bash
# Installed command
nuanic-ring-monitor --calibration-seconds 60
```

### Multi-ring

```bash
nuanic-ring-monitor --monitor-all --target-hz 16 --mode live
nuanic-ring-monitor --ring-addrs MAC1,MAC2 --target-hz 16 --reset-bt --mode raw_eda
```

### Operational Modes

```bash
# Live Mode (default) — responsive DNE + instant indicator on d306262b
nuanic-ring-monitor --mode live

# Research Mode — stable, long-filter DNE on d306262b
nuanic-ring-monitor --mode research

# Raw EDA Mode — pure unprocessed EDA in Ohms on 42dcb71b (no onboard DNE)
nuanic-ring-monitor --mode raw_eda

# Standby — physiology OFF, IMU + finger-detect still active
nuanic-ring-monitor --mode standby
```

### Discover Services

```bash
nuanic-ring-discover --ring-addr AA:BB:CC:DD:EE:FF --profile-seconds 15
```

## Switching Operational Modes

You can switch the ring's operational mode in three ways: CLI, Python SDK, or raw BLE GATT.

### 1. Via the CLI (`nuanic-ring-monitor`)

```bash
# Raw EDA mode — 14-byte Ohms stream on 42dcb71b (no onboard DNE)
nuanic-ring-monitor --mode raw_eda

# Live Mode — responsive DNE + instant indicator on d306262b (default)
nuanic-ring-monitor --mode live

# Research Mode — stable long-filter DNE on d306262b
nuanic-ring-monitor --mode research

# Standby — physiology OFF, IMU + finger-detect still active
nuanic-ring-monitor --mode standby
```

### 2. Via the Python SDK (`NuanicConnector`)

```python
import asyncio
from nuanic_ring import NuanicConnector, MODE_RAW_EDA, MODE_LIVE, MODE_RESEARCH, MODE_STANDBY

async def main():
    connector = NuanicConnector()
    mac = "7C:2F:61:5F:E0:4F"
    await connector.connect_device(mac)

    await connector.set_mode(MODE_RAW_EDA)   # 0x01 — Raw EDA only
    # or MODE_LIVE, MODE_RESEARCH, MODE_STANDBY

asyncio.run(main())
```

With `NuanicMonitor`, set `initial_mode` on init:

```python
from nuanic_ring import NuanicMonitor, MODE_RAW_EDA
monitor = NuanicMonitor(initial_mode=MODE_RAW_EDA, csv_layout="combined")
```

### 3. Under the Hood (BLE GATT)

Write a single `uint8_t` to `STORAGE_FORMAT_UUID` (`3cce21a7-e602-4e02-8c52-1e0366c1c846`):

| Target Mode | Byte Value |
|---|---|
| `MODE_STANDBY` | `0x00` |
| `MODE_RAW_EDA` | `0x01` |
| `MODE_LIVE` | `0x02` |
| `MODE_RESEARCH` | `0x03` |

> ⚠️ **60-Second Calibration Window:** Every mode transition triggers a mandatory 60-second internal calibration. Physiological streams are muted during this period. Writing the same mode the ring is already in is a no-op.

---

## Python API Usage

### Simple timed run

```python
import asyncio
from nuanic_ring.monitor import NuanicMonitor

async def run_once():
    monitor = NuanicMonitor(calibration_seconds=60, mode="live")
    await monitor.run(duration_seconds=120)

asyncio.run(run_once())
```

### Explicit lifecycle (recommended for multi-ring orchestration)

```python
import asyncio
from nuanic_ring import NuanicMonitor, MODE_LIVE, MODE_RAW_EDA

async def run_multi():
    monitor = NuanicMonitor(target_hz=16, calibration_seconds=60, initial_mode=MODE_RAW_EDA)
    started = await monitor.start_multi(
        ring_addresses=["MAC1", "MAC2"],
        auto_reconnect=True,
    )
    if not started:
        return

    try:
        await asyncio.sleep(120)
    finally:
        await monitor.stop_multi()

asyncio.run(run_multi())
```

## Ring Profiles

Two profiles can appear in the field:
- Nuanic profile (primary target in this repository)
- Moodmetric profile

Use profile-aware diagnostics when behavior is unclear:

```bash
nuanic-ring-discover --subscribe-core-streams --ring-profile auto
```

### One-shot commands (no streaming)

```bash
# Check flash memory usage
nuanic-ring-monitor --check-flash --ring-addr AA:BB:CC:DD:EE:FF

# Sync ring clock to system time
nuanic-ring-monitor --sync-time --ring-addr AA:BB:CC:DD:EE:FF

# Reset onboard DNE algorithm
nuanic-ring-monitor --reset-algo --ring-addr AA:BB:CC:DD:EE:FF

# Download offline session records
nuanic-ring-monitor --download-storage --ring-addr AA:BB:CC:DD:EE:FF

# Put ring into shipping mode (disconnects battery until docked)
nuanic-ring-monitor --shipping-mode --ring-addr AA:BB:CC:DD:EE:FF
```

## Troubleshooting

- If connect fails with stale Windows BLE state, retry with `--reset-bt`.
- Prefer `--target-hz 16` for multi-ring stability unless intentionally stress-testing.
- Use `--scan-attempts` and `--scan-timeout` to improve discovery reliability in noisy environments.
- If logs look empty at startup, remember logging is lazy-started after first payload.

## What Lives Where

- [README.md](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/README.md): Quick start, installation, multi-ring commands, and code usage.
- [docs/ring_master_guide.md](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_master_guide.md) (this file): Operational playbook, full CLI argument references, and GATT UUID mappings.
- [docs/csv_format.md](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/csv_format.md): Detail of output CSV columns, record types, physical conversions, and pandas parsing.
- [docs/ring_reverse_engineering_report.md](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_reverse_engineering_report.md): Low-level packet forensics, byte-mapping, and discovery diagnostics.

---

## 🛠️ CLI Argument Reference (`nuanic-ring-monitor`)

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--duration` | Total session length in seconds. | Unlimited |
| `--ring-addrs` | Comma-separated list of MAC addresses to connect. | None |
| `--monitor-all` | Connect to all discovered Nuanic rings. | False |
| `--target-hz` | Desired sampling frequency in Hz (capped between 1 and 16 Hz). | 10.0 |
| `--force-hz` | Bypass the 16Hz hardware capability safety warning. | False |
| `--reset-bt` | Aggressively reset Windows BT radio on initial failure. | False |
| `--log` / `--no-log` | Enable or disable CSV recording. | `--log` |
| `--log-dir` | Folder for session CSV output. | `data/ring_logs` |
| `--waveform` | Launch live Matplotlib plots instead of the TUI table. | False |
| `--markers` | Enable runtime marker input (SPACE and single-key hotkeys, plus `/m LABEL` + Enter). | False |
| `--marker-hotkey` | Add or override a single-key marker hotkey. Repeatable. | `SPACE=marker, S=stimulus_on, B=baseline_start, R=rest_start` |
| `--post-analysis` | Print a scoring comparison vs proprietary DNE on exit. | No |
| `--use-warmup` | Enable legacy disconnect/reconnect priming cycle. | False |
| `--stagger-delay` | Seconds to wait between connecting multiple rings. | 1.25 |
| `--auto-reconnect` | Automaticaly retry on connection drop. | True |
| `--calibration-seconds` | Wait time for Arousal Scorer baseline window. | 60 |
| `--imu-refresh` | Batch size for dashboard IMU signal updates. | 5 |
| `--ui-refresh-ms` | Dashboard UI redraw interval. | 200ms |
| `--rate-control` | Attempt to write sample-rate configuration to ring. | `yes` |
| `--equalize-mode` | Logic for handling rate mismatches (`off`, `log-only`, `enforce`). | `log-only` |
| `--max-devices` | Cap the number of simultaneously monitored rings. | None |
| `--scan-timeout` | Timeout per scan attempt. | 6.0s |
| `--scan-attempts` | Number of scan attempts before giving up. | 3 |
| `--warmup-delay` | Delay after firmware warmup before full connect. | 3.0s |
| `--mode` | Ring operational mode: `live` (0x02), `research` (0x03), `raw_eda` (0x01), `standby` (0x00). | `live` |
| `--list-rings` | Scan and list available rings, then exit. | - |
| `--discover` | Full GATT service discovery and characteristics dump. | - |
| `--check-flash` | Check available and used flash storage on the ring. | - |
| `--sync-time` | Force send real-time clock synchronization timestamp to ring. | - |
| `--reset-algo` | Send ASCII command `"ra"` to reset onboard DNE algorithm. | - |
| `--shipping-mode` | Send ASCII command `"sm"` to put ring in shipping mode (disconnects battery). | - |
| `--download-storage` | Download offline session storage records from ring memory. | - |

---

## 🔑 GATT UUID Mapping 

The verified GATT characteristic meanings are:

| UUID | Decoded Name | SDK Attribute / Alias | Verified Interpretation & Struct Format |
|---|---|---|---|
| `5491faaf-b0c2-4167-8f3d-bc6b31db69e7` | **Nuanic Service** | `NUANIC_SERVICE_UUID` | Proprietary GATT Service for Nuanic / Moodmetric rings |
| `3c180fcc-bfec-4b7c-8e52-1a37f123e449` | **State** | `STATE_UUID` | Read/Notify state indicator: `0`: Init, `1`: Off finger, `2`: On finger, `3`: Docked |
| `516b0fb6-d861-4619-9dd0-0105e8b85128` | **Sample rate** | `SAMPLE_RATE_UUID` (`CONFIG_1`) | Read/Write sample rate: any of `3, 4, 6, 8, 12, 16` as `uint8_t` |
| `dc9c31a7-fbd3-467a-8777-10900c423d3b` | **Realtime** | `REALTIME_UUID` (`CONFIG_2`) | Write device time: Unix timestamp in ms (`uint64_t` LE, `<Q`) |
| `d306262b-c8c9-4c4b-9050-3a41dea706e5` | **Live DNE** | `LIVE_DNE_UUID` / `LIVE_DNA_UUID` | Notify/Read 16 bytes (`<Qii`): `{uint64_t timestamp; int32_t instant; int32_t dne;}` |
| `42dcb71b-1817-43bd-8ea3-7272780a1c9f` | **Live EDA** | `LIVE_EDA_UUID` / `ALGO_1MIN_UUID` | Notify 14 bytes (`<HQI`): `{uint16_t boot_count; uint64_t timestamp; uint32_t eda_ohm;}` |
| `7c3b82e7-22b7-4cb6-8458-ba325edf6ede` | **Storage** | `STORAGE_UUID` (`BUFFER_UUID`) | Read MTU chunks. Format 1 (14B `<HQI`): EDA. Format 2 (22B `<HQiii`): DNE + SRRN + SRL |
| `3cce21a7-e602-4e02-8c52-1e0366c1c846` | **Storage format** | `STORAGE_FORMAT_UUID` (`CONFIG_3`) | Read/Write format (`uint8_t`): `0`: None (Standby), `1`: Raw EDA, `2`: Nuanic Algorithm |
| `2175c13f-60e4-4de5-80af-0d06f1b54880` | **Storage rewind** | `STORAGE_REWIND_UUID` (`WRITE_1`) | Write 10 bytes (`<HQ`): `{uint16_t boot_count; uint64_t timestamp;}` to rewind flash reading |
| `d78e5bd8-53d6-4fc3-bc98-03b8cd71684b` | **Storage usage** | `STORAGE_USAGE_UUID` | Read 8 bytes (`<II`): `{uint32_t size; uint32_t used;}` for available and used flash memory |
| `741f0d15-cc3d-4715-a9fb-a5a6bccebc50` | **Command** | `COMMAND_UUID` | Write 2 ASCII bytes: `"sm"` (shipping mode) or `"ra"` (reset algorithm) |
| `468f2717-6a7d-46f9-9eb7-f92aab208bae` | **IMU Batch** | `IMU_BATCH_UUID` | Bulk motion / IMU batch stream (14-sample batches of X, Y, Z at ~1Hz) |



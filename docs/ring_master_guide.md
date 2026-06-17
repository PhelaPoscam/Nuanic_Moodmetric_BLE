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
nuanic-ring-monitor --monitor-all --target-hz 16
nuanic-ring-monitor --ring-addrs MAC1,MAC2 --target-hz 16 --reset-bt
```

### Analyze Logs

```bash
nuanic-ring-analyzer data/ring_logs/my_session.csv
nuanic-ring-post-analysis --latest 2
```

### Discover Services

```bash
nuanic-ring-discover --ring-addr AA:BB:CC:DD:EE:FF --profile-seconds 15
```

## Python API Usage

### Simple timed run

```python
import asyncio
from nuanic_ring.monitor import NuanicMonitor

async def run_once():
    monitor = NuanicMonitor(calibration_seconds=60)
    await monitor.run(duration_seconds=120)

asyncio.run(run_once())
```

### Explicit lifecycle (recommended for multi-ring orchestration)

```python
import asyncio
from nuanic_ring.monitor import NuanicMonitor

async def run_multi():
    monitor = NuanicMonitor(target_hz=16, calibration_seconds=60)
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
| `--list-rings` | Scan and list available rings, then exit. | - |
| `--discover` | Full GATT service discovery and characteristics dump. | - |

---

## 🔑 GATT UUID Mapping

The verified GATT characteristic meanings are:

| UUID | Current label | Verified interpretation |
|---|---|---|
| `3c180fcc-bfec-4b7c-8e52-1a37f123e449` | `STATE_CHARACTERISTIC` | Off-finger / on-finger state indicator stream |
| `7c3b82e7-22b7-4cb6-8458-ba325edf6ede` | `STORAGE_UUID` | Historical storage / buffer characteristic |
| `42dcb71b-1817-43bd-8ea3-7272780a1c9f` | `LIVE_EDA_UUID` | Live notify stream (no reliable payload) |
| `d306262b-c8c9-4c4b-9050-3a41dea706e5` | `LIVE_DNA_UUID` / `STRESS_CHARACTERISTIC` | High-rate physiological stream (raw EDA + Stress Index) at ~16Hz |
| `468f2717-6a7d-46f9-9eb7-f92aab208bae` | `IMU_CHARACTERISTIC` | Bulk motion / IMU batch stream (14-sample batches at ~1Hz) |
| `dc9c31a7-fbd3-467a-8777-10900c423d3b` | `SET_TIME` | Writable config/timestamp register |
| `516b0fb6-d861-4619-9dd0-0105e8b85128` | `SAMPLE_RATE` | Writable config register; rate-write effect is proven |
| `3cce21a7-e602-4e02-8c52-1e0366c1c846` | `STORAGE_FORMAT` | Writable config register |


# Nuanic & Moodmetric Ring BLE SDK

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![CI/CD](https://github.com/PhelaPoscam/Nuanic_Moodmetric_BLE/actions/workflows/ci.yml/badge.svg)](https://github.com/PhelaPoscam/Nuanic_Moodmetric_BLE/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A Python library for connecting, monitoring, and capturing raw electrodermal activity (EDA) and IMU waveforms from **Nuanic** and legacy **Moodmetric** BLE rings. Includes a real-time **Moodmetric-like Arousal Scoring** pipeline and a live Matplotlib dashboard.

---

## 🚀 Quick Start

### 1. Installation

```bash
python -m venv .venv
# Windows
.\.venv\Scripts\Activate.ps1
# Linux/Mac
source .venv/bin/activate

pip install -e ".[dev]"
```

### 2. Connect & Monitor

```bash
# Quick start
nuanic-ring-monitor --calibration-seconds 60

# Live dashboard visualization
nuanic-ring-monitor --waveform --calibration-seconds 60

# Monitor all discovered rings
nuanic-ring-monitor --monitor-all --calibration-seconds 15

# Multi-ring by MAC addresses
nuanic-ring-monitor --ring-addrs MAC1,MAC2 --target-hz 16

# Session with analysis (DNE vs computed score)
nuanic-ring-monitor --monitor-all --duration 60 --post-analysis yes

# Session with runtime stimulus markers
nuanic-ring-monitor --monitor-all --markers --calibration-seconds 15
# During run, press SPACE or S/B/R for instant markers
# Or type: /m STIMULUS_NAME + Enter

# Custom hotkeys
nuanic-ring-monitor --markers --marker-hotkey S=stimulus_on --marker-hotkey B=baseline_start --marker-hotkey R=rest_start

# Offline analysis
nuanic-ring-analyzer data/ring_logs/my_session.csv
nuanic-ring-post-analysis --latest 2
nuanic-ring-discover --ring-addr 56:C2:72:F2:07:04 --profile-seconds 15
```

---

## 🔗 Multi-Ring Setup

### Explicit Targeting
```bash
nuanic-ring-monitor --ring-addrs MAC1,MAC2 --target-hz 16 --reset-bt
```

### Auto-Discovery
```bash
nuanic-ring-monitor --monitor-all --target-hz 16
```

For troubleshooting details, profile-specific notes, and connection recovery strategy, see `docs/ring_master_guide.md`.

---

## 🛠️ CLI Argument Reference (`ring_monitor_cli.py`)

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

## 🛠️ Usage in Code

```python
import asyncio
from nuanic_ring.monitor import NuanicMonitor

async def run_sensor():
    monitor = NuanicMonitor(calibration_seconds=60)
    # Run the monitor for 120 seconds
    await monitor.run(duration_seconds=120)

asyncio.run(run_sensor())
```

Advanced lifecycle (explicit multi-ring control):

```python
import asyncio
from nuanic_ring.monitor import NuanicMonitor

async def run_multi_sensor():
  monitor = NuanicMonitor(calibration_seconds=60, target_hz=16)
  started = await monitor.start_multi(
    ring_addresses=["41:09:FB:6B:95:8D", "69:1D:C9:2E:19:64"],
    auto_reconnect=True,
  )
  if not started:
    return
  try:
    await asyncio.sleep(120)
  finally:
    await monitor.stop_multi()

asyncio.run(run_multi_sensor())
```

---

## 📖 Documentation

- **Hardware Reverse-Engineering:** [Ring Reverse-Engineering Report](docs/ring_reverse_engineering_report.md)
- **Ring Integration API & Master Guide:** [Ring Master Guide](docs/ring_master_guide.md)

## UUID Mapping

The verified GATT characteristic meanings are:

| UUID | Current label | Verified interpretation |
|---|---|---|
| `3c180fcc-bfec-4b7c-8e52-1a37f123e449` | `STATE_CHARACTERISTIC` | Off-finger / on-finger state indicator stream |
| `7c3b82e7-22b7-4cb6-8458-ba325edf6ede` | `STORAGE_UUID` | Historical storage / buffer characteristic |
| `42dcb71b-1817-43bd-8ea3-7272780a1c9f` | `LIVE_EDA_UUID` | Live notify stream (no reliable payload) |
| `d306262b-c8c9-4c4b-9050-3a41dea706e5` | `IMU_STREAM` | High-rate motion / physiology stream |
| `dc9c31a7-fbd3-467a-8777-10900c423d3b` | `SET_TIME` | Writable config/timestamp register |
| `516b0fb6-d861-4619-9dd0-0105e8b85128` | `SAMPLE_RATE` | Writable config register; rate-write effect is proven |
| `3cce21a7-e602-4e02-8c52-1e0366c1c846` | `STORAGE_FORMAT` | Writable config register |

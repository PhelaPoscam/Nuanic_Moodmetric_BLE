# Nuanic & Moodmetric Ring BLE SDK

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![CI/CD](https://github.com/PhelaPoscam/Nuanic_Moodmetric_BLE/actions/workflows/ci.yml/badge.svg)](https://github.com/PhelaPoscam/Nuanic_Moodmetric_BLE/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)

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
# Monitor the first matched ring and log to CSV with arousal scoring
python scripts/ring_monitor_cli.py --calibration-seconds 60

# Live EDA + Arousal Score GUI dashboard
python scripts/ring_monitor_cli.py --waveform --calibration-seconds 60

# Monitor all visible rings at once
python scripts/ring_monitor_cli.py --monitor-all --calibration-seconds 15

# Explicit two-ring monitoring by MAC addresses
python scripts/ring_monitor_cli.py --ring-addrs 41:09:FB:6B:95:8D,69:1D:C9:2E:19:64 --duration 120 --target-hz 16 --reset-bt

# Run a short session and auto-print DNE-vs-computed score analysis
python scripts/ring_monitor_cli.py --monitor-all --duration 60 --post-analysis yes

# Discover exactly which proprietary GATT profile is active on your ring
python scripts/ring_monitor_cli.py --discover

# Offline analysis of a recorded session CSV
python scripts/ring_analyzer_cli.py data/ring_logs/my_session.csv

# Quick analysis over latest 2 ring CSVs
python scripts/ring_post_analysis_cli.py --latest 2
```

---

## 🏗️ Architecture Overview

The system follows an **Event-Driven / Producer-Consumer** model built on `asyncio`.

```
BLE Ring
  │  (GATT Notify @ ~25 Hz)
  ▼
NuanicConnector      ← Manages BLE lifecycle, pairing, Windows WinRT hacks
  │
  ▼
NuanicMonitor        ← Orchestrates profile detection, parsing, logging, TUI
  │
  ├─▶ SignalConditioner   ← Median filter + Butterworth Low-Pass (noise rejection)
  │       │
  │       ▼
  │   MMLikeScorer        ← Real-time 1-100 Arousal Score from SCR features
  │
  ├─▶ CSV Logger          ← Persistent session recording (21-column schema)
  └─▶ TUI Display         ← ANSI live terminal dashboard
```

### Core Modules (`src/nuanic_ring/`)

| Module | Purpose |
|--------|---------|
| `connector.py` | BLE discovery, connection, GATT subscription |
| `monitor.py` | Main orchestration, parsing, logging, TUI |
| `waveform_viewer.py` | Live Matplotlib GUI (Raw EDA, Filtered uS, Arousal, IMU) |
| `signal_processing.py` | `SignalConditioner`: Median + Butterworth filtering |
| `mm_compat.py` | `MMLikeScorer`: Moodmetric-compatible 1-100 arousal scoring |
| `data_analysis.py` | Offline vectorized CSV analysis (Pandas/NumPy/scipy) |
| `ring_profiles.py` | GATT UUID definitions per ring profile |
| `moodmetric_parser.py` | Legacy Moodmetric byte-payload parser |

---

## 📊 Real-Time Arousal Scoring Pipeline

The scoring pipeline runs on every EDA packet:

1. **Raw EDA** → `SignalConditioner` (Median kernel=3, Butterworth 1.5 Hz LP)
2. **Filtered conductance (μS)** → `MMLikeScorer.update_scr_features()` → detects SCR peaks
3. **SCR frequency + amplitude** → `MMLikeScorer.update()` → outputs **Arousal Score 1-100**

The scorer calibrates over the first 60 seconds before emitting meaningful scores.

The real-time dashboard shows two parallel stress/arousal signals:

1. Our computed score (`Our Arousal (1-100)`) from `MMLikeScorer`.
2. Ring proprietary sender value (`Ring DNE (0-100)`) parsed from the D306 stream.

This makes side-by-side comparison possible during live sessions.

---

## 📈 Post-Session Score Comparison

`ring_post_analysis_cli.py` compares proprietary `Stress_Index` (DNE) against
`MM_Arousal_Score` from your pipeline for the latest ring logs.

Reported metrics per file:

1. Pearson correlation (`corr`)
2. Mean offset (`ours - dne`)
3. Best lag in samples (`best_lag_samples`) and corresponding lag correlation

Example:

```bash
python scripts/ring_post_analysis_cli.py --log-dir data/ring_logs --latest 2
```

---

## 🔬 Empirical Rate Findings & Connection Stability

From repeated paired-ring sessions (same command pattern, two MACs), the
observed behavior on the Nuanic hardware is:

1. **Hardware Rate Ceiling:** Multi-ring environments hit a hard capability wall at **~16.0 Hz**. The firmware struggles with higher frequencies (like 25 Hz), dropping sync and eventually crashing. We've capped the default safety limits at 16 Hz.
2. **Setup Stabilization:** The rings do not necessarily require complex "disconnect/reconnect" warmup phases. They just need around **30-90 seconds** after the initial connection to stabilize internally, acknowledge the requested rate, and begin broadcasting steady payload buffers.
3. **Lazy Logging:** To prevent storing 90 seconds of empty "ramp-up" data, the monitor uses **Lazy Logging**. CSV files are only actively created and written to when the first true payload broadcast is captured.
4. **Bluetooth Hang Recovery (`--reset-bt`)**: Under heavy loads on Windows, WinRT links can "ghost". If the initial ring connection fails, passing `--reset-bt` lets the orchestrator immediately trigger an aggressive Windows radio-level cycle, ensuring the phantom link drops and the ring is securely connected on retry.

Recommended robust default for two-ring capture at max frequency:

```bash
python scripts/ring_monitor_cli.py --ring-addrs <MAC1>,<MAC2> --target-hz 16 --reset-bt --duration 120
```

---

## 🛠️ Usage in Code

```python
import asyncio
from nuanic_ring.monitor import NuanicMonitor

async def run_sensor():
    monitor = NuanicMonitor(calibration_seconds=60)
    await monitor.start_monitoring(duration_seconds=120)

asyncio.run(run_sensor())
```

---

## 📖 Documentation

- **Hardware Reverse-Engineering:** [Ring Reverse-Engineering Report](docs/ring_reverse_engineering_report.md)
- **Ring Integration API & Master Guide:** [Ring Master Guide](docs/ring_master_guide.md)

## UUID Mapping

The current code keeps the old UUIDs, but the verified best-fit meanings are:

| UUID | Current label | Verified interpretation |
|---|---|---|
| `3c180fcc-bfec-4b7c-8e52-1a37f123e449` | `STATE_CHARACTERISTIC` / `RAW_EDA_CHARACTERISTIC` | Off-finger / on-finger state indicator stream |
| `7c3b82e7-22b7-4cb6-8458-ba325edf6ede` | `STORAGE_UUID` | Historical storage / buffer characteristic |
| `42dcb71b-1817-43bd-8ea3-7272780a1c9f` | `LIVE_EDA_UUID` | Live notify stream (currently no reliable payload) |
| `d306262b-c8c9-4c4b-9050-3a41dea706e5` | `LIVE_DNA_UUID` / IMU stream | High-rate motion / physiology stream |
| `dc9c31a7-fbd3-467a-8777-10900c423d3b` | `SET_TIME_UUID` | Writable config/timestamp register |
| `516b0fb6-d861-4619-9dd0-0105e8b85128` | `SAMPLE_RATE_UUID` | Writable config register; rate-write effect still unproven |
| `3cce21a7-e602-4e02-8c52-1e0366c1c846` | `STORAGE_FORMAT_UUID` | Writable config register |

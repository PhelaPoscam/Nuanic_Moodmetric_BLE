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

# Discover exactly which proprietary GATT profile is active on your ring
python scripts/ring_monitor_cli.py --discover

# Offline analysis of a recorded session CSV
python scripts/ring_analyzer_cli.py data/ring_logs/my_session.csv
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

---

## 🔭 Future Research Directions

1. **LSL (Lab Streaming Layer) Integration** — Broadcast EDA/Arousal to EEG/eye-tracking rigs for multi-modal research.
2. **Motion Artifact Rejection** — Use high-frequency IMU data to mask EDA spikes caused by movement.
3. **Cross-Ring Synchronization** — Connect two rings simultaneously to study bilateral electrodermal asymmetry.

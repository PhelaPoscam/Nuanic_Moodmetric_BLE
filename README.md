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
# Start standard monitoring (lazy-logs session data to CSV)
nuanic-ring-monitor --calibration-seconds 60

# Launch live dashboard visualization
nuanic-ring-monitor --waveform

# Run post-session analysis on a log
nuanic-ring-analyzer data/ring_logs/my_session.csv
```

---

## 🔗 Multi-Ring Setup

```bash
# Connect to all discovered Nuanic rings
nuanic-ring-monitor --monitor-all --target-hz 16

# Explicitly target specific MAC addresses
nuanic-ring-monitor --ring-addrs MAC1,MAC2 --target-hz 16 --reset-bt
```

---

## 🛠️ Usage in Code

```python
import asyncio
from nuanic_ring.monitor import NuanicMonitor

async def run_sensor():
    # Initialize monitor with a 60-second baseline calibration window
    monitor = NuanicMonitor(calibration_seconds=60)
    
    # Run the monitor for 120 seconds
    await monitor.run(duration_seconds=120)

asyncio.run(run_sensor())
```

For advanced multi-ring orchestration, see [Python API Usage in the Master Guide](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_master_guide.md#python-api-usage).

---

## 📖 Documentation Directory

Refer to the detailed documents below for deep dives into SDK features, file formats, and hardware interpretations:

*   📂 **[CSV Log Format Guide](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/csv_format.md)**: Detailed breakdown of the output CSV columns, record types (`D306_EDA`, `IMU_BATCH_468F`, etc.), scaling formulations, and offline Pandas parsing.
*   📂 **[Ring Master Guide](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_master_guide.md)**: Setup workflows, advanced multi-ring controls, troubleshooting, and the **[Full CLI Argument Reference](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_master_guide.md#%EF%B8%8F-cli-argument-reference-nuanic-ring-monitor)** & **[GATT UUID Mapping](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_master_guide.md#-gatt-uuid-mapping)** tables.
*   📂 **[Ring Reverse-Engineering Report](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_reverse_engineering_report.md)**: Low-level BLE forensics, profile validations, and raw characteristic payload structures.

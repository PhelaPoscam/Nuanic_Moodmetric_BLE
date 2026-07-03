# Nuanic & Moodmetric Ring BLE SDK

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![CI/CD](https://github.com/PhelaPoscam/Nuanic_Moodmetric_BLE/actions/workflows/ci.yml/badge.svg)](https://github.com/PhelaPoscam/Nuanic_Moodmetric_BLE/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A Python library for connecting, monitoring, and capturing raw electrodermal activity (EDA) and IMU waveforms from **Nuanic** and legacy **Moodmetric** BLE rings. Includes **operational mode switching** (Standby / Raw EDA / Live / Research), sample rate control (3–16 Hz), real-time **DNE arousal scoring**, and a live Matplotlib dashboard.

---

## 🚀 Quick Start

### 1. Installation

#### Option A: Core Library Install (Recommended for Library Use)
To install the core BLE SDK with minimal dependencies (only `bleak` and `scipy`):
```bash
pip install nuanic-ring
```

#### Option B: Library with CLI & Tooling Install (Recommended for CLI Use)
To install the package along with the interactive terminal dashboard, waveform viewer, and offline logs analysis tools (which pull in `rich`, `matplotlib`, `pandas`, and `numpy`):
```bash
pip install "nuanic-ring[cli]"
```

#### Option C: Local Developer Install
For active development (editable install with all testing and CLI/tools dependencies):
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
# Start monitoring in Live mode (responsive DNE) at 16 Hz
nuanic-ring-monitor --calibration-seconds 60 --target-hz 16 --mode live

# Switch to Raw EDA mode — pure Ohms, no onboard DNE computation
nuanic-ring-monitor --mode raw_eda --target-hz 16

# Launch live dashboard visualization
nuanic-ring-monitor --waveform

# Run post-session analysis on a log
nuanic-ring-analyzer data/ring_logs/SessionDate_YYYY-MM-DD_HH-MM-SS/csvs/ring--MAC.csv

# Output exact Nuanic sample format CSV with SRL and SRRN
nuanic-ring-monitor --nuanic-export --log

# One-shot commands (no streaming)
nuanic-ring-monitor --check-flash --ring-addr AA:BB:CC:DD:EE:FF
nuanic-ring-monitor --sync-time --ring-addr AA:BB:CC:DD:EE:FF
nuanic-ring-monitor --reset-algo --ring-addr AA:BB:CC:DD:EE:FF
```

---

## 🔗 Multi-Ring Setup

```bash
# Connect to all discovered Nuanic rings in Live mode
nuanic-ring-monitor --monitor-all --target-hz 16 --mode live

# Explicitly target specific MAC addresses in Raw EDA mode
nuanic-ring-monitor --ring-addrs MAC1,MAC2 --target-hz 16 --mode raw_eda --reset-bt
```

---

## 🛠️ Usage in Code

```python
import asyncio
from nuanic_ring import NuanicConnector, MODE_LIVE, MODE_RAW_EDA, MODE_RESEARCH, MODE_STANDBY

async def run_sensor():
    connector = NuanicConnector()
    await connector.connect()

    # Set operational mode (always triggers a 60s calibration window)
    await connector.set_mode(MODE_LIVE)       # Preprocessed instant + DNE on d306
    await connector.set_sample_rate(16)       # 3–16 Hz

    # One-shot utility commands
    await connector.sync_time()               # Sync ring clock to real time
    usage = await connector.read_storage_usage()  # Check flash: size, used, available
    records = await connector.download_storage()  # Download offline sessions

    # System commands
    await connector.send_command("ra")        # Reset DNE algorithm
    await connector.send_command("sm")        # Shipping mode (battery disconnect)

    await connector.disconnect()

asyncio.run(run_sensor())
```

### Operational Modes

The ring has 4 modes controlled via `CONFIG_3` register. Switch modes via `--mode` on the CLI or `set_mode()` in code:

```bash
nuanic-ring-monitor --mode raw_eda    # Pure Ohms, no onboard DNE
nuanic-ring-monitor --mode live       # Responsive DNE (default)
nuanic-ring-monitor --mode research   # Stable, long-filter DNE
nuanic-ring-monitor --mode standby    # Physiology OFF
```

| Constant | Value | Active Stream | What you get |
|---|---|---|---|
| `MODE_STANDBY` | `0x00` | — | Physiology OFF (IMU + finger-detect stay active) |
| `MODE_RAW_EDA` | `0x01` | `42dcb71b` (14-byte `<HQI`) | Raw skin resistance in Ohms — **no onboard DNE**. Use when doing your own DSP. |
| `MODE_LIVE` | `0x02` | `d306262b` (16-byte `<Qii`) | Preprocessed instant indicator + DNE score. **Short filter** — responsive to changes. |
| `MODE_RESEARCH` | `0x03` | `d306262b` (16-byte `<Qii`) | Preprocessed instant indicator + DNE score. **Long filter** — stable, reproducible. |

> ⚠️ Every mode transition triggers a **60-second silent calibration window**. All physiological BLE streams are muted during this period. Writing the same mode the ring is already in is a no-op.

For the full reverse-engineering breakdown, see [Ring Reverse-Engineering Report](docs/ring_reverse_engineering_report.md).

---

## 📖 Documentation Directory

Refer to the detailed documents below for deep dives into SDK features, file formats, and hardware interpretations:

*   📂 **[CSV Log Format Guide](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/csv_format.md)**: Detailed breakdown of the output CSV columns, record types (`D306_EDA`, `IMU_BATCH_468F`, etc.), scaling formulations, and offline Pandas parsing.
*   📂 **[Ring Master Guide](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_master_guide.md)**: Setup workflows, advanced multi-ring controls, troubleshooting, and the **[Full CLI Argument Reference](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_master_guide.md#%EF%B8%8F-cli-argument-reference-nuanic-ring-monitor)** & **[GATT UUID Mapping](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_master_guide.md#-gatt-uuid-mapping)** tables.
*   📂 **[Ring Reverse-Engineering Report](file:///c:/Code%20-%20Projects/Python%20Projects/Nuanic_Moodmetric_BLE/docs/ring_reverse_engineering_report.md)**: Low-level BLE forensics, profile validations, and raw characteristic payload structures.

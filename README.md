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
To install the package along with the interactive terminal dashboard and waveform viewer (which pull in `rich`, `matplotlib`, and `numpy`):
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
# Start monitoring in Raw EDA mode (default) — unfiltered skin resistance in Ohms
nuanic-ring-monitor --target-hz 16

# Use Live mode for onboard DNE stress index (preprocessed, firmware-filtered)
nuanic-ring-monitor --target-hz 16 --mode live

# Launch live dashboard visualization
nuanic-ring-monitor --waveform

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
# Connect to all discovered Nuanic rings in Raw EDA mode (default)
nuanic-ring-monitor --monitor-all --target-hz 16

# Explicitly target specific MAC addresses in Live mode (onboard DNE)
nuanic-ring-monitor --ring-addrs MAC1,MAC2 --target-hz 16 --mode live --reset-bt
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
nuanic-ring-monitor --mode raw_eda    # Pure Ohms, no onboard DNE (default)
nuanic-ring-monitor --mode live       # Responsive DNE
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

### Phasic EDA (SCR) Notes

For phasic analysis (SCR detection, onset latency, amplitude):

- Use `MODE_RAW_EDA` (default) — `MODE_LIVE` / `MODE_RESEARCH` return a firmware-preprocessed "instant" indicator, not raw Ohms.
- The default `nuanic-ring-monitor` does NOT apply any host-side filter. Pass `--filter` to opt in to the median + 1.5 Hz Butterworth lowpass. (`--raw` is deprecated and ignored — it was the old way to bypass the filter, which is now the default.)
- Hardware ceiling: max sample rate is **16 Hz** — adequate for SCR amplitude and latency, marginal for fast waveform morphology.
- The onboard DNE score is a tonic-level measure, not phasic. It does not show individual SCR events.

---

## 🏛️ SDK Architecture

The codebase is organized into decoupled submodules designed for high throughput, low jitter, and modular extension:

```
src/nuanic_ring/
├── core/             # BLE connection lifecycle, GATT profiles, and radio discovery
│   ├── connector.py  # NuanicConnector (connect, subscribe, commands, offline storage)
│   ├── scanner.py    # RingScanner (discovery & Windows Bluetooth radio management)
│   └── profiles.py   # GATT UUIDs, register signatures, and profile detection
├── telemetry/        # Telemetry ingestion, hardware time reconstruction & state tracking
│   ├── device_state.py # RingDeviceState (thread-safe multi-ring live state dataclass)
│   ├── time_sync.py  # TimeSynchronizer (ring clock unwrapping & smoothing)
│   ├── rate_control.py # SamplingRateController (sample rate monitoring & throttle)
│   └── callbacks.py  # Raw BLE notification packet decoders
├── io/               # Strongly-typed schemas, streaming CSV writers, and provenance
│   ├── schemas.py    # Dataclasses for packets (EdaPacket, ImuBatchPacket) & CSV rows
│   ├── manifest.py   # Cryptographic session manifest generator (SHA-256 validation)
│   └── writers.py    # Async non-blocking CSV writer loops
├── dsp/              # Real-time DSP signal processing
│   └── signal_processing.py # SignalConditioner (Median + 2nd-order Butterworth low-pass)
└── monitor.py        # NuanicMonitor coordinator facade & rich TUI dashboard
```

---

## 🔒 Scientific Data Provenance & Manifests

Every live recording session automatically generates a cryptographic session manifest (`session_manifest.json`) alongside your session CSV logs:

- **Hardware Metadata**: Ring MAC, firmware revision, serial number, boot count, and initial battery level.
- **Session Provenance**: Start and stop ISO-8601 timestamps, duration, total packet count, and observed sample rate.
- **Cryptographic File Hashes**: SHA-256 checksums generated for all exported CSV logs (`eda.csv`, `imu.csv`, `finger.csv`, `session.csv`) to guarantee data integrity in research pipelines.

---

## 📓 Interactive Jupyter Notebooks

Hands-on tutorials and analysis recipes are included in the `examples/` directory:

1. **[01_quickstart_stream.ipynb](examples/01_quickstart_stream.ipynb)**: Connect to a ring, set operational modes, and stream live EDA and motion telemetry with real-time callbacks.
2. **[02_eda_and_motion_analysis.ipynb](examples/02_eda_and_motion_analysis.ipynb)**: Load multi-stream CSV logs with Pandas, inspect data provenance via `session_manifest.json`, apply dual-stage DSP filters, and perform phasic SCR and motion artifact detection.
3. **[03_offline_flash_download.ipynb](examples/03_offline_flash_download.ipynb)**: Inspect onboard NOR flash memory, download offline historical session records, and plot autonomous wearable trends.

---

## 📖 Documentation Directory

Refer to the detailed documents below for deep dives into SDK features, file formats, and hardware interpretations:

*   📂 **[CSV Log Format Guide](docs/csv_format.md)**: Detailed breakdown of the output CSV columns, record types (`D306_EDA`, `LIVE_EDA_42DC`, etc.), scaling formulations, and offline Pandas parsing.
*   📂 **[Ring Master Guide](docs/ring_master_guide.md)**: Setup workflows, advanced multi-ring controls, troubleshooting, and the **[Full CLI Argument Reference](docs/ring_master_guide.md#%EF%B8%8F-cli-argument-reference-nuanic-ring-monitor)** & **[GATT UUID Mapping](docs/ring_master_guide.md#-gatt-uuid-mapping)** tables.
*   📂 **[Ring Reverse-Engineering Report](docs/ring_reverse_engineering_report.md)**: Low-level BLE forensics, profile validations, and raw characteristic payload structures.


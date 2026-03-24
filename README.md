# Nuanic & Moodmetric Ring BLE SDK

An independent, lightweight Python library for connecting, monitoring, and capturing raw electrodermal activity (EDA) and IMU waveforms from **Nuanic** and legacy **Moodmetric** BLE rings.

---

## 🚀 Quick Start

### 1. Installation

This library relies strictly on standard Bluetooth Low Energy (`bleak`) and data-frame handling capabilities.
```bash
python -m venv venv
# Windows
.\venv\Scripts\Activate.ps1
# Linux/Mac
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Connect & Monitor
To instantly connect, list rings, or view a live waveform:
```bash
# Monitor the first matched ring and log to CSV
python scripts/ring_monitor_cli.py --duration 60

# Discover exactly which proprietary GATT profile is active on your ring
python scripts/discover_ring_services.py 

# Live EDA Waveform visualization
python scripts/ring_monitor_cli.py --waveform
```

---

## 📖 Deep-Dive Documentation

Please read the reverse-engineering logs to understand how the payloads are structured, and what the BLE strings map to:
- **Hardware Reverse-Engineering (Nuanic & Moodmetric):** [Ring Reverse-Engineering Report](docs/ring_reverse_engineering_report.md)
- **Ring Integration API & Master Guide:** [Ring Master Guide](docs/ring_master_guide.md)

---

## 🛠️ Usage in Code

```python
import asyncio
from nuanic_ring.monitor import NuanicMonitor

async def run_sensor():
    monitor = NuanicMonitor()
    # Auto-discovers Nuanic/Moodmetric rings, connects, and starts recording!
    await monitor.start_monitoring(duration_seconds=60)
    
asyncio.run(run_sensor())
```

See the files inside `scripts/` for extensive examples on logging, session extraction, and API usage.

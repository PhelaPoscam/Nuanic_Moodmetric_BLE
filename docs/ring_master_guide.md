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

# Script path equivalent
python scripts/ring_monitor_cli.py --calibration-seconds 60
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

- `README.md`: onboarding, command examples, full monitor CLI argument table, UUID mapping.
- `docs/ring_master_guide.md` (this file): operational playbook.
- `docs/ring_reverse_engineering_report.md`: packet-level reverse-engineering and historical interpretation notes.

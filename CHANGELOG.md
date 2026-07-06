# Changelog

All notable changes to this project are documented in this file.

## Unreleased — 2026-07-03

### Removed
- `nuanic-ring-analyzer` and `nuanic-ring-post-analysis` CLI entry points, along with `data_analysis.py` and `post_analysis.py` modules — offline CSV analysis and algorithm curve-fitting are obsolete as the SDK focuses on core BLE data extraction and monitoring.
- `scripts/` directory (3 probe scripts) — one-off RE experiments replaced by the cleaner `NuanicConnector` API.
- `moodmetric_parser.py` — legacy Moodmetric UUID parser; no internal consumers, no tests.

### Added
- **Mode switching API** on `NuanicConnector`:
  - `set_mode(mode)` — write to `CONFIG_3` to switch operational modes
  - `set_sample_rate(hz)` — write to `CONFIG_1` (3–16 Hz)
  - `read_buffer()` — read flash storage buffer (`7c3b82e7`)
  - Mode constants: `MODE_STANDBY`, `MODE_RAW_EDA`, `MODE_LIVE`, `MODE_RESEARCH`
- `NuanicMonitor` accepts `initial_mode` parameter for mode-on-connect

### Added (July 2026 firmware update)
- `sync_time()` — auto-sync on connect + manual CLI `--sync-time` (`dc9c31a7`, `<Q`)
- `read_storage_usage()` + `--check-flash` CLI (`d78e5bd8`, `<II`)
- `download_storage()` + `--download-storage` CLI (MTU chunk loop, Format 1 `<HQI` / Format 2 `<HQiii`)
- `rewind_storage()` — dev-only flash pointer rewind (`2175c13f`, `<HQ`)
- `send_command("sm"|"ra")` + `--reset-algo` / `--shipping-mode` CLI (`741f0d15`)
- Live EDA parser for `42dcb71b` (`<HQI`: boot_count, timestamp_ms, eda_ohm)
- Live DNE parser updated to `<Qii` (timestamp_ms, instant, dne)

### Discovered
- **Complete 4-state CONFIG_3 map**: Standby (0x00), Raw EDA (0x01), Live (0x02), Research (0x03)
- **Universal 60-second calibration law**: every mode transition mutes BLE streams for 60s
- **0x02 vs 0x03 difference**: DNE filter window — short/responsive vs long/conservative
- **42dcb71b is raw EDA only** — 14-byte `<HQI`, no onboard DNE computation in Mode 0x01
- **d306262b is preprocessed DNE** — 16-byte `<Qii` (instant indicator normalised ~1e6 + DNE)
- **CONFIG_1 controls sample rate universally** (3–16 Hz) across all active modes
- **CONFIG_2 is real-time clock** — write Unix ms (`uint64_t` LE) after each boot
- **Offline storage protocol** — `7c3b82e7` reads in MTU chunks; Format 1 = EDA, Format 2 = DNE+SRRN+SRL

### Changed
- Mode constants renamed to reflect firmware spec (`MODE_ALGO` → `MODE_RAW_EDA`, `MODE_EDA` → `MODE_LIVE`, etc.)
- Old names kept as backward-compat aliases
- Report updated with `<HQI` and `<Qii` payload structures
- `convert_eda()` docstring now notes it produces legacy-derived values when fed d306 `instant`

## 0.1.1 - 2026-04-13

### Added
- Installable CLI entrypoints via `pyproject.toml` scripts:
  - `nuanic-ring-monitor`
  - `nuanic-ring-analyzer`
  - `nuanic-ring-post-analysis`
  - `nuanic-ring-discover`
- `src/nuanic_ring/cli_entrypoints.py` launchers for script-based CLIs.
- Contract tests to prevent README/CLI drift:
  - CLI default/alias contracts
  - scan default/signature contracts
  - entrypoint script path checks

### Changed
- README trimmed for clarity and focused onboarding.
- README now preserves and aligns:
  - usage examples
  - monitor CLI argument reference
  - UUID mapping
- Deep operational notes moved from README to `docs/ring_master_guide.md`.
- CI now runs `pytest -q` in addition to Black formatting checks.
- Black target version aligned to project Python baseline (`py310`).
- Scripts now use package imports (no `sys.path.insert(...)` bootstrap).

### Fixed
- Windows dashboard fallback in monitor CLI hardened for encoding edge cases.
- Scan timeout/attempt defaults made consistent through connector/monitor/CLI paths.

### Docs
- Removed transient remediation artifact doc.
- Reduced redundancy across docs by separating operational guide vs reverse-engineering report.

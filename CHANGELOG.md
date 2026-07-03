# Changelog

All notable changes to this project are documented in this file.

## Unreleased — 2026-07-03

### Added
- **Mode switching API** on `NuanicConnector`:
  - `set_mode(mode)` — write to `CONFIG_3` to switch operational modes
  - `set_sample_rate(hz)` — write to `CONFIG_1` (3–16 Hz)
  - `read_buffer()` — read flash storage buffer (`7c3b82e7`)
  - Mode constants: `MODE_STANDBY`, `MODE_RAW_EDA`, `MODE_LIVE`, `MODE_RESEARCH`
- `NuanicMonitor` accepts `initial_mode` parameter for mode-on-connect
- **RE probe scripts** in `scripts/`:
  - `probe_mode_switch.py` — systematic long-window mode-switch probing
  - `probe_algo_stream.py` — capture & decode 42dcb71b ALGO stream
  - `probe_flash_storage.py` — dual-mode A/B comparison (0x02 vs 0x03)

### Discovered
- **Complete 4-state CONFIG_3 map**: Standby (0x00), Raw EDA (0x01), Live (0x02), Research (0x03)
- **Universal 60-second calibration law**: every mode transition mutes BLE streams for 60s
- **0x02 vs 0x03 difference**: DNE filter window — short/responsive vs long/conservative
- **42dcb71b is raw EDA only** — no onboard DNE computation in Mode 0x01
- **CONFIG_1 controls sample rate universally** (3–16 Hz) across all active modes
- **CONFIG_2 is a free-running millisecond clock**, not mode-dependent
- Flash storage trigger not yet found — neither 0x02 nor 0x03 writes to buffer

### Changed
- Mode constants renamed to reflect verified behavior (`MODE_ALGO` → `MODE_RAW_EDA`, etc.)
- Old names kept as backward-compat aliases
- Report updated with verified payload structures and experimental data

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

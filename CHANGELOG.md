# Changelog

All notable changes to this project are documented in this file.

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

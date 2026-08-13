# ADR-003: Multi-ring session Hz safety cap

- **Status:** Accepted
- **Date:** 2026-08-13 (decision made earlier in project history; recorded here)
- **Context:** Multi-device monitoring sessions were observed to be unstable above
  ~16 Hz due to hardware/BLE throughput limitations. Requests above 16 Hz for
  multi-ring sessions either silently degrade or risk dropped packets.
- **Decision:** `start_multi` caps `target_hz` to 16.0 Hz when the session is
  multi-ring (`monitor_all=True` or >1 explicit address), unless `--force-hz` is
  passed. Single-ring sessions are not capped.
- **Consequences:**
  - Multi-ring sessions get a stability guarantee at the cost of a lower max rate.
  - `--force-hz` is an explicit, logged danger override (see
    `_apply_multi_ring_hz_cap` in `monitor.py`).
- **Alternatives considered:** No cap (rejected: instability); per-device rate
  control only (rejected: the limitation is aggregate BLE throughput).

# ADR-001: Eager CSV log-file initialization

- **Status:** Accepted
- **Date:** 2026-08-13
- **Context:** Repowise health analysis flagged `io_in_loop` / `blocking_sync_in_async`
  findings in `monitor.py`: the BLE notify callbacks (the per-packet hot path)
  performed blocking filesystem I/O (`open()`/`mkdir()`) on the first packet via
  lazy `_enqueue_*` → `_initialize_*` → `_open_log_file`.
- **Decision:** Log files and their writer tasks are created eagerly when a device
  state is created *inside a running session* (`self.running`), so the callback
  path only does queue enqueues. When not running (pre-session, sync callers),
  initialization falls back to lazy on first enqueue. Writer creation is
  loop-aware: without a running event loop it no-ops and is retried by the next
  async init call.
- **Consequences:**
  - Callbacks never block on the filesystem; queue-full drops (`dropped_rows`)
    are the only loss path, and they are reported at session end.
  - Log files exist (header written) even for sessions that receive no packets.
  - The `running` gate means writers are never started before a session, avoiding
    writer loops that exit immediately (`while self.running or not queue.empty()`).
- **Alternatives considered:** Keep lazy init (rejected: blocking I/O in callback);
  use `asyncio.to_thread` for file I/O (rejected: overkill for once-per-session
    open, and the writer loop already batches writes off the callback).

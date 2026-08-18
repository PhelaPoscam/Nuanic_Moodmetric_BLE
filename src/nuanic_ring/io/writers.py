"""Asynchronous batch CSV writers and file initialization utilities."""

from __future__ import annotations

import asyncio
import csv
import logging
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

_log = logging.getLogger(__name__)


def build_log_filename(
    mac: str, suffix: str = "", participant_id: Optional[str] = None
) -> str:
    """Construct sanitized session CSV filename."""
    safe_mac = mac.replace(":", "-")
    parts: List[str] = []
    if participant_id:
        parts.append(participant_id)
    parts.append(f"ring-{safe_mac[-6:]}")
    if suffix:
        parts.append(suffix)
    return "_".join(parts) + ".csv"


def open_csv_log_file(
    log_dir: Path,
    session_timestamp: str,
    mac: str,
    suffix: str,
    header: Sequence[str],
    participant_id: Optional[str] = None,
    enabled: bool = True,
) -> Optional[Path]:
    """Create session CSV log file with header row."""
    if not enabled:
        return None
    filename = build_log_filename(mac, suffix, participant_id)
    session_folder = log_dir / f"SessionDate_{session_timestamp}" / "csvs"
    session_folder.mkdir(parents=True, exist_ok=True)
    file_path = session_folder / filename
    try:
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(header)
        _log.info("Started log for %s: %s", mac, filename)
        return file_path
    except Exception as e:
        _log.error("Error initializing log for %s: %s", mac, e)
        return None


async def csv_writer_loop(
    is_running: Callable[[], bool],
    queue: asyncio.Queue[List[Any]],
    log_file: Path,
    batch_size: int = 64,
    timeout: float = 0.2,
) -> None:
    """Asynchronous background loop writing batches of rows to CSV."""
    if not log_file or not queue:
        return

    batch: List[List[Any]] = []
    while is_running() or not queue.empty():
        try:
            row = await asyncio.wait_for(queue.get(), timeout=timeout)
            batch.append(row)
            if len(batch) < batch_size:
                continue
        except asyncio.TimeoutError:
            pass

        if not batch:
            continue

        try:
            with open(log_file, "a", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerows(batch)
        except Exception:
            _log.debug("CSV write error for %s", log_file, exc_info=True)
        batch.clear()


async def drain_writer_tasks(tasks: Sequence[Optional[asyncio.Task[None]]]) -> None:
    """Wait for background writer tasks to finish draining queues."""
    for task in tasks:
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass

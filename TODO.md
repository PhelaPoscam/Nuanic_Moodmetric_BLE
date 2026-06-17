# TODO — ponytail debt ledger

Items deferred because the current code works and the ceiling hasn't been hit.

## Split connector.py
**File:** `src/nuanic_ring/connector.py`
**What:** ~1,000 lines — scanning, discovery, reconnect, BT radio reset, address cache all in one class.
**When:** Split into `_scanner.py` / `_connection.py` when navigation becomes painful.

## Fold discover_services.py
**File:** `src/nuanic_ring/discover_services.py`
**What:** 641 lines, duplicates BLE connection lifecycle and arg parsing from connector.py / cli.py.
**When:** Fold into cli.py or share the connector when the duplication causes a real bug.

## Fix waveform threading
**File:** `src/nuanic_ring/waveform_viewer.py`
**What:** Matplotlib updates use threading instead of asyncio. Mixing the two causes heisenbugs.
**When:** Switch to a matplotlib async backend (e.g. Qt) or FigureCanvasAgg + manual blit when this breaks.

## Add type checker to CI
**File:** `.github/workflows/ci.yml`
**What:** No mypy/pyright in CI. Binary BLE packet decoders with struct.unpack would benefit.
**When:** Add when an offset bug slips through that a type checker would've caught.

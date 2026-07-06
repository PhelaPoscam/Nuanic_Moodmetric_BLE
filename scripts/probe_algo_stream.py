#!/usr/bin/env python3
"""Capture and decode the 42dcb71b ALGO stream (Mode 0x01).

The 14-byte packet structure is verified:

    [0-1]   boot_count    — uint16 LE
    [2-9]   timestamp_ms  — uint64 LE (Unix epoch milliseconds)
    [10-13] eda_ohm       — uint32 LE (skin resistance in Ohms)

This script captures every packet and logs the known fields plus
derived conductance for offline analysis.
"""

import argparse
import asyncio
import csv
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from nuanic_ring.connector import NuanicConnector
    from nuanic_ring.discover_services import print_header
except ModuleNotFoundError:
    print("[ERROR] Install the project first: pip install -e .[dev]")
    raise SystemExit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Capture & decode the 42dcb71b ALGO stream."
    )
    p.add_argument("--ring-addr", default=None, help="BLE MAC address")
    p.add_argument(
        "--capture-duration",
        type=float,
        default=120.0,
        help="Seconds to capture AFTER the 60s calibration (default: 120).",
    )
    p.add_argument(
        "--out-dir",
        default="data/algo_probes",
        help="Output directory for CSV (default: data/algo_probes).",
    )
    return p.parse_args()


def decode_eda(eda_ohm: int) -> dict:
    """Derive resistance and conductance from raw EDA in Ohms."""
    resistance_kohm = eda_ohm / 1000.0
    conductance_us = (1000000.0 / eda_ohm) if eda_ohm > 0 else 0.0
    return {
        "resistance_kohm": resistance_kohm,
        "conductance_us": conductance_us,
    }


async def main() -> int:
    args = parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"algo_probe_{ts}.csv"

    print_header("ALGO STREAM PROBE — 42dcb71b Decoding")
    print(f"Mode: 0x01 (ALGO)")
    print(f"Calibration: 60s")
    print(f"Capture: {args.capture_duration}s")
    print(f"Output: {csv_path}")

    connector = NuanicConnector(
        target_address=args.ring_addr,
        unpair_on_disconnect=False,
        max_connect_attempts=3,
        connect_backoff_seconds=2.0,
        pair_on_connect=True,
    )

    try:
        if not await connector.connect():
            print("[FAIL] Could not connect to ring.")
            return 1

        print(f"[CONNECTED] {connector.target_address}")

        # ── Subscribe to 42dc ──────────────────────────────────────
        algo_uuid = "42dcb71b-1817-43bd-8ea3-7272780a1c9f"
        packets: list[tuple[float, bytes]] = []

        def on_algo(_sender, data):
            packets.append((time.time(), bytes(data)))

        await connector.client.start_notify(algo_uuid, on_algo)
        print(f"[SUB] {algo_uuid}")

        # ── Write mode 0x01 ────────────────────────────────────────
        print_header("WRITING MODE 0x01 (ALGO)")
        ok = await connector.set_mode(NuanicConnector.MODE_RAW_EDA)
        if not ok:
            print("[FAIL] Mode write failed.")
            return 1
        print("[OK] 60s calibration window begins...")

        # ── Wait calibration + capture ─────────────────────────────
        total_wait = 60.0 + args.capture_duration
        t0 = time.time()
        last_report = 0

        while (elapsed := time.time() - t0) < total_wait:
            await asyncio.sleep(5.0)
            elapsed = time.time() - t0
            pkt_count = len(packets)
            recent = pkt_count - last_report
            last_report = pkt_count
            phase = "CALIBRATING" if elapsed < 60 else "CAPTURING"
            print(
                f"  t={elapsed:5.1f}s | {phase} | "
                f"pkts this window: {recent:3d} | total: {pkt_count:4d}"
            )

        # ── Cleanup ────────────────────────────────────────────────
        try:
            await connector.client.stop_notify(algo_uuid)
        except Exception:
            pass

        await connector.set_mode(NuanicConnector.MODE_STANDBY)

        # ── Decode & write CSV ─────────────────────────────────────
        print_header(f"DECODING {len(packets)} PACKETS")

        if not packets:
            print("[WARN] No packets captured. Was the ring on-finger?")
            return 1

        # Show a few raw samples
        print("\nRaw samples (first 5):")
        for i, (t, data) in enumerate(packets[:5]):
            print(f"  [{i}] t={t:.3f} | {data.hex()}")

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "elapsed_s",
                    "unix_time",
                    "raw_hex",
                    "boot_count",
                    "timestamp_ms",
                    "eda_ohm",
                    "resistance_kohm",
                    "conductance_us",
                ]
            )

            for t, data in packets:
                elapsed = t - t0
                boot_count, timestamp_ms, eda_ohm = struct.unpack("<HQI", data)
                d = decode_eda(eda_ohm)

                writer.writerow(
                    [
                        f"{elapsed:.3f}",
                        f"{t:.6f}",
                        data.hex(),
                        boot_count,
                        timestamp_ms,
                        eda_ohm,
                        f"{d['resistance_kohm']:.4f}",
                        f"{d['conductance_us']:.4f}",
                    ]
                )

        # ── Summary statistics ─────────────────────────────────────
        print_header("SUMMARY STATISTICS")

        eda_vals = []
        timestamps = []

        for _, data in packets:
            _boot, ts, eda = struct.unpack("<HQI", data)
            timestamps.append(ts)
            eda_vals.append(eda)

        if eda_vals:
            unique_eda = len(set(eda_vals))
            cond_vals = [decode_eda(e)["conductance_us"] for e in eda_vals]
            print(
                f"  EDA (Ohm)    | min={min(eda_vals):12d} | max={max(eda_vals):12d} | "
                f"unique={unique_eda:5d} | first={eda_vals[0]} | last={eda_vals[-1]}"
            )
            print(
                f"  Conduct. (uS)| min={min(cond_vals):12.4f} | max={max(cond_vals):12.4f} | "
                f"first={cond_vals[0]:.4f} | last={cond_vals[-1]:.4f}"
            )

        # Timestamp / rate diagnostics
        if len(timestamps) > 1:
            ts_deltas = [
                timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))
            ]
            avg_delta_ms = sum(ts_deltas) / len(ts_deltas)
            print(
                f"\n  Timestamp deltas: min={min(ts_deltas)} max={max(ts_deltas)} "
                f"avg={avg_delta_ms:.1f} ms"
            )
            if avg_delta_ms > 0:
                print(f"  Effective rate: {1000.0 / avg_delta_ms:.1f} Hz")

        print(f"\n[DONE] Full decode written to: {csv_path}")
        return 0

    except KeyboardInterrupt:
        print("\n[STOP] Interrupted.")
        return 1
    finally:
        await connector.disconnect()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(1)

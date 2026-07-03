#!/usr/bin/env python3
"""Capture and brute-force decode the 42dcb71b ALGO stream (Mode 0x01).

The 14-byte packet structure is NOT verified.  Known layout:

    [0-1]  0600         — header (constant)
    [2-5]  XXXXXXXX     — clock / timestamp (uint32 LE, monotonic)
    [6-9]  00000000     — context? (always zero in captures)
    [10-13] YYYYYYYY    — MYSTERY PAYLOAD

The Nuanic manual claims bytes 10-13 encode Average DNE + SRL + SRRN,
which is implausible in 4 bytes without packed encoding.  This script
captures every packet and decodes the mystery field as:

    • uint32 LE    • int32 LE     • float32 LE
    • 2 × int16 LE  • 2 × uint16 LE

All output goes to a timestamped CSV for offline analysis.
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
        "--capture-duration", type=float, default=120.0,
        help="Seconds to capture AFTER the 60s calibration (default: 120)."
    )
    p.add_argument(
        "--out-dir", default="data/algo_probes",
        help="Output directory for CSV (default: data/algo_probes)."
    )
    return p.parse_args()


def decode_mystery(val_bytes: bytes) -> dict:
    """Try every plausible decoding of the 4-byte mystery field."""
    u32 = struct.unpack("<I", val_bytes)[0]
    i32 = struct.unpack("<i", val_bytes)[0]
    try:
        f32 = struct.unpack("<f", val_bytes)[0]
    except Exception:
        f32 = float("nan")

    i16_0 = struct.unpack("<h", val_bytes[0:2])[0]
    i16_1 = struct.unpack("<h", val_bytes[2:4])[0]
    u16_0 = struct.unpack("<H", val_bytes[0:2])[0]
    u16_1 = struct.unpack("<H", val_bytes[2:4])[0]

    return {
        "u32": u32,
        "i32": i32,
        "f32": f32,
        "i16_0": i16_0,
        "i16_1": i16_1,
        "u16_0": u16_0,
        "u16_1": u16_1,
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
            print(f"  t={elapsed:5.1f}s | {phase} | "
                  f"pkts this window: {recent:3d} | total: {pkt_count:4d}")

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
            writer.writerow([
                "elapsed_s", "unix_time",
                "raw_hex",
                "header_u16",
                "clock_u32",
                "ctx_u32",
                "mystery_u32", "mystery_i32", "mystery_f32",
                "mystery_i16_0", "mystery_i16_1",
                "mystery_u16_0", "mystery_u16_1",
            ])

            for t, data in packets:
                elapsed = t - t0
                header = struct.unpack("<H", data[0:2])[0]
                clock = struct.unpack("<I", data[2:6])[0]
                ctx = struct.unpack("<I", data[6:10])[0]
                mystery = data[10:14]
                d = decode_mystery(mystery)

                writer.writerow([
                    f"{elapsed:.3f}", f"{t:.6f}",
                    data.hex(),
                    header, clock, ctx,
                    d["u32"], d["i32"], d["f32"],
                    d["i16_0"], d["i16_1"],
                    d["u16_0"], d["u16_1"],
                ])

        # ── Summary statistics ─────────────────────────────────────
        print_header("SUMMARY STATISTICS")

        u32_vals = []
        i16_0_vals = []
        i16_1_vals = []
        u16_0_vals = []
        u16_1_vals = []
        clocks = []

        for _, data in packets:
            clocks.append(struct.unpack("<I", data[2:6])[0])
            d = decode_mystery(data[10:14])
            u32_vals.append(d["u32"])
            i16_0_vals.append(d["i16_0"])
            i16_1_vals.append(d["i16_1"])
            u16_0_vals.append(d["u16_0"])
            u16_1_vals.append(d["u16_1"])

        def stats(name, vals):
            if not vals:
                return
            unique = len(set(vals))
            print(f"  {name:12s} | min={min(vals):12d} | max={max(vals):12d} | "
                  f"unique={unique:5d} | first={vals[0]} | last={vals[-1]}")

        stats("uint32", u32_vals)
        stats("i16_0", i16_0_vals)
        stats("i16_1", i16_1_vals)
        stats("u16_0", u16_0_vals)
        stats("u16_1", u16_1_vals)

        # Clock diagnostics
        if len(clocks) > 1:
            clock_deltas = [clocks[i] - clocks[i-1] for i in range(1, len(clocks))]
            avg_delta = sum(clock_deltas) / len(clock_deltas)
            print(f"\n  Clock deltas: min={min(clock_deltas)} max={max(clock_deltas)} "
                  f"avg={avg_delta:.1f}")
            if avg_delta > 0:
                print(f"  Effective rate: {1.0 / (avg_delta / 1000):.1f} Hz "
                      f"(assuming clock is ms)")

        # Heuristic: which decoding looks like DNE (0–100 range)?
        print_header("DNE RANGE HEURISTIC (looking for 0–100 values)")
        for label, vals in [
            ("uint32", u32_vals), ("i16_0", i16_0_vals), ("i16_1", i16_1_vals),
            ("u16_0", u16_0_vals), ("u16_1", u16_1_vals),
        ]:
            in_range = sum(1 for v in vals if 0 <= v <= 100)
            pct = 100 * in_range / len(vals) if vals else 0
            print(f"  {label:12s}: {in_range}/{len(vals)} values in 0–100 ({pct:.0f}%)")

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

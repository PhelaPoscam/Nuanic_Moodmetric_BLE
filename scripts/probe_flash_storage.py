#!/usr/bin/env python3
"""Comprehensive dual-mode probe: 0x02 vs 0x03 back-to-back in one session.

Tests BOTH hypotheses simultaneously:
  1. Flash storage — does buffer (7c3b82e7) grow after 0x03 but not 0x02?
  2. DNE filter  — do DNE scores differ between modes under same conditions?

Protocol:
  1. Connect, subscribe to d306 + 42dc + state (3c18)
  2. Baseline buffer read + register snapshot
  3. Mode 0x02 → 60s calib → stream N seconds → log everything
  4. Standby → Mode 0x03 → 60s calib → stream N seconds → log everything
  5. Standby → final buffer read + register snapshot
  6. Dump full CSV with per-packet decode + session summary

Output: data/flash_probes/probe_<timestamp>.csv
"""

import argparse
import asyncio
import csv
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from nuanic_ring.connector import NuanicConnector
    from nuanic_ring.discover_services import (
        WRITE_READ_CHARS,
        print_header,
    )
except ModuleNotFoundError:
    print("[ERROR] Install the project first: pip install -e .[dev]")
    raise SystemExit(1)

# ── GATT UUIDs ─────────────────────────────────────────────────────
D306_UUID = "d306262b-c8c9-4c4b-9050-3a41dea706e5"
ALGO_UUID = "42dcb71b-1817-43bd-8ea3-7272780a1c9f"
STATE_UUID = "3c180fcc-bfec-4b7c-8e52-1a37f123e449"
IMU_UUID = "468f2717-6a7d-46f9-9eb7-f92aab208bae"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Dual-mode probe: 0x02 vs 0x03 in one session."
    )
    p.add_argument("--ring-addr", default=None, help="BLE MAC address")
    p.add_argument(
        "--stream-duration",
        type=float,
        default=90.0,
        help="Seconds to stream AFTER each 60s calibration (default: 90).",
    )
    p.add_argument(
        "--out-dir",
        default="data/flash_probes",
        help="Output directory for CSV (default: data/flash_probes).",
    )
    return p.parse_args()


async def read_registers(client) -> dict:
    states = {}
    for uuid, label in WRITE_READ_CHARS.items():
        try:
            val = await client.read_gatt_char(uuid)
            states[label] = bytes(val).hex()
        except Exception as exc:
            states[label] = f"ERR({exc})"
    return states


async def main() -> int:
    args = parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"dual_mode_probe_{ts}.csv"
    buf_dir = out_dir / f"buffers_{ts}"
    buf_dir.mkdir(parents=True, exist_ok=True)

    # ── Packet capture ring buffers ─────────────────────────────────
    # Each entry: (unix_time, mode_label, stream_uuid_short, raw_hex, decoded_fields)
    all_packets: list[dict] = []

    stream_pkt_counts = {"d306": 0, "42dc": 0, "3c18": 0, "468f": 0}
    current_mode = "init"

    def make_cb(stream_tag: str):
        def cb(_sender, data):
            payload = bytes(data)
            stream_pkt_counts[stream_tag] += 1
            entry = {
                "unix": time.time(),
                "mode": current_mode,
                "stream": stream_tag,
                "hex": payload.hex(),
                "len": len(payload),
            }
            # Decode known formats
            if stream_tag == "d306" and len(payload) == 16:
                ts_ms, instant, dne = struct.unpack("<Qii", payload)
                entry["timestamp_ms"] = ts_ms
                entry["instant"] = instant
                entry["dne"] = dne
            elif stream_tag == "42dc" and len(payload) == 14:
                entry["header"] = struct.unpack("<H", payload[0:2])[0]
                entry["clock"] = struct.unpack("<I", payload[2:6])[0]
                entry["ctx"] = struct.unpack("<I", payload[6:10])[0]
                entry["mystery_u16_0"] = struct.unpack("<H", payload[10:12])[0]
                entry["mystery_u16_1"] = struct.unpack("<H", payload[12:14])[0]
                entry["mystery_u32"] = struct.unpack("<I", payload[10:14])[0]
            elif stream_tag == "3c18" and len(payload) == 1:
                entry["state"] = payload[0]
            elif stream_tag == "468f" and len(payload) == 92:
                entry["imu_clock"] = struct.unpack("<I", payload[0:4])[0]
                entry["imu_ctx"] = struct.unpack("<I", payload[4:8])[0]
            all_packets.append(entry)

        return cb

    # ── Connect ─────────────────────────────────────────────────────
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

        mac = connector.target_address
        print(f"[CONNECTED] {mac}")

        # ── Subscribe to all streams ─────────────────────────────────
        for uuid, tag in [
            (D306_UUID, "d306"),
            (ALGO_UUID, "42dc"),
            (STATE_UUID, "3c18"),
            (IMU_UUID, "468f"),
        ]:
            try:
                await connector.client.start_notify(uuid, make_cb(tag))
                print(f"[SUB] {tag} — {uuid[:8]}...")
            except Exception as exc:
                print(f"[SUB FAIL] {tag}: {exc}")

        # ── BASELINE ─────────────────────────────────────────────────
        print_header("BASELINE — BEFORE ANY MODE CHANGE")
        buf_bl = await connector.read_buffer()
        buf_bl_len = len(buf_bl) if buf_bl else 0
        print(f"[BUFFER] {buf_bl_len} bytes")
        if buf_bl and buf_bl_len > 0:
            (buf_dir / "baseline.bin").write_bytes(buf_bl)

        regs_bl = await read_registers(connector.client)
        print(f"[REGISTERS] {regs_bl}")
        await asyncio.sleep(3.0)  # brief baseline capture

        # ── PHASE 1: Mode 0x02 ───────────────────────────────────────
        print_header("PHASE 1: MODE 0x02 (Real-Time EDA)")
        current_mode = "0x02"
        stream_pkt_counts = {k: 0 for k in stream_pkt_counts}

        await connector.set_mode(NuanicConnector.MODE_LIVE)
        regs_after_02 = await read_registers(connector.client)
        print(f"[REGISTERS AFTER 0x02 WRITE] {regs_after_02}")

        # 60s calibration wait
        print("[CALIB] Waiting 60s calibration window...")
        t0 = time.time()
        while (elapsed := time.time() - t0) < 60.0:
            await asyncio.sleep(10.0)
            elapsed = time.time() - t0
            print(
                f"  calib t={elapsed:5.1f}s | "
                f"d306={stream_pkt_counts['d306']:4d} "
                f"42dc={stream_pkt_counts['42dc']:4d} "
                f"state={stream_pkt_counts['3c18']:4d} "
                f"imu={stream_pkt_counts['468f']:4d}"
            )

        # Stream window
        pkt_snapshot_02 = stream_pkt_counts.copy()
        print(f"\n[STREAM] 0x02 streaming for {args.stream_duration}s...")
        t0 = time.time()
        while (elapsed := time.time() - t0) < args.stream_duration:
            await asyncio.sleep(10.0)
            elapsed = time.time() - t0
            recent = {
                k: stream_pkt_counts[k] - pkt_snapshot_02[k] for k in stream_pkt_counts
            }
            # Print latest d306 packet if available
            extra = ""
            d306_pkts = [
                p for p in all_packets if p["mode"] == "0x02" and p["stream"] == "d306"
            ]
            if d306_pkts:
                last = d306_pkts[-1]
                extra = f"| last_d306: instant={last.get('instant','?')} DNE={last.get('dne','?')} ts={last.get('timestamp_ms','?')}"
            print(
                f"  stream t={elapsed:5.1f}s | "
                f"d306={recent['d306']:4d}(+{stream_pkt_counts['d306']}) "
                f"42dc={recent['42dc']:4d} "
                f"state={recent['3c18']:4d} "
                f"imu={recent['468f']:4d} {extra}"
            )

        print(
            f"\n[0x02 TOTAL] d306={stream_pkt_counts['d306']} "
            f"42dc={stream_pkt_counts['42dc']} "
            f"state={stream_pkt_counts['3c18']} "
            f"imu={stream_pkt_counts['468f']}"
        )

        # ── SWITCH TO STANDBY BRIEFLY ────────────────────────────────
        print_header("INTERMISSION — STANDBY")
        await connector.set_mode(NuanicConnector.MODE_STANDBY)
        await asyncio.sleep(3.0)
        buf_mid = await connector.read_buffer()
        buf_mid_len = len(buf_mid) if buf_mid else 0
        print(f"[BUFFER AFTER 0x02] {buf_mid_len} bytes (baseline was {buf_bl_len})")
        if buf_mid and buf_mid_len > 0:
            (buf_dir / "after_0x02.bin").write_bytes(buf_mid)

        # ── PHASE 2: Mode 0x03 ───────────────────────────────────────
        print_header("PHASE 2: MODE 0x03 (Real-Time EDA Variant)")
        current_mode = "0x03"
        stream_pkt_counts = {k: 0 for k in stream_pkt_counts}

        await connector.set_mode(NuanicConnector.MODE_RESEARCH)
        regs_after_03 = await read_registers(connector.client)
        print(f"[REGISTERS AFTER 0x03 WRITE] {regs_after_03}")

        # 60s calibration wait
        print("[CALIB] Waiting 60s calibration window...")
        t0 = time.time()
        while (elapsed := time.time() - t0) < 60.0:
            await asyncio.sleep(10.0)
            elapsed = time.time() - t0
            print(
                f"  calib t={elapsed:5.1f}s | "
                f"d306={stream_pkt_counts['d306']:4d} "
                f"42dc={stream_pkt_counts['42dc']:4d} "
                f"state={stream_pkt_counts['3c18']:4d} "
                f"imu={stream_pkt_counts['468f']:4d}"
            )

        # Stream window
        pkt_snapshot_03 = stream_pkt_counts.copy()
        print(f"\n[STREAM] 0x03 streaming for {args.stream_duration}s...")
        t0 = time.time()
        while (elapsed := time.time() - t0) < args.stream_duration:
            await asyncio.sleep(10.0)
            elapsed = time.time() - t0
            recent = {
                k: stream_pkt_counts[k] - pkt_snapshot_03[k] for k in stream_pkt_counts
            }
            extra = ""
            d306_pkts = [
                p for p in all_packets if p["mode"] == "0x03" and p["stream"] == "d306"
            ]
            if d306_pkts:
                last = d306_pkts[-1]
                extra = f"| last_d306: instant={last.get('instant','?')} DNE={last.get('dne','?')} ts={last.get('timestamp_ms','?')}"
            print(
                f"  stream t={elapsed:5.1f}s | "
                f"d306={recent['d306']:4d}(+{stream_pkt_counts['d306']}) "
                f"42dc={recent['42dc']:4d} "
                f"state={recent['3c18']:4d} "
                f"imu={recent['468f']:4d} {extra}"
            )

        print(
            f"\n[0x03 TOTAL] d306={stream_pkt_counts['d306']} "
            f"42dc={stream_pkt_counts['42dc']} "
            f"state={stream_pkt_counts['3c18']} "
            f"imu={stream_pkt_counts['468f']}"
        )

        # ── FINAL: Standby + buffer read ─────────────────────────────
        print_header("FINAL — STANDBY + BUFFER CHECK")
        await connector.set_mode(NuanicConnector.MODE_STANDBY)
        await asyncio.sleep(2.0)

        buf_final = await connector.read_buffer()
        buf_final_len = len(buf_final) if buf_final else 0
        print(
            f"[BUFFER FINAL] {buf_final_len} bytes "
            f"(baseline={buf_bl_len}, after_0x02={buf_mid_len})"
        )
        if buf_final and buf_final_len > 0:
            (buf_dir / "after_0x03.bin").write_bytes(buf_final)
            print(f"[BUFFER HEX] first 128: {buf_final[:128].hex()}")
            if buf_final_len > 128:
                print(f"[BUFFER HEX] last 128:  {buf_final[-128:].hex()}")

        regs_final = await read_registers(connector.client)
        print(f"[FINAL REGISTERS] {regs_final}")

        # ── DNE COMPARISON ───────────────────────────────────────────
        print_header("DNE COMPARISON: 0x02 vs 0x03")
        dne_02 = [
            p["dne"]
            for p in all_packets
            if p["mode"] == "0x02" and p["stream"] == "d306" and "dne" in p
        ]
        dne_03 = [
            p["dne"]
            for p in all_packets
            if p["mode"] == "0x03" and p["stream"] == "d306" and "dne" in p
        ]

        def summarize(label, vals):
            if not vals:
                print(f"  {label}: NO DATA")
                return
            print(
                f"  {label}: n={len(vals)} "
                f"min={min(vals)} max={max(vals)} "
                f"mean={sum(vals)/len(vals):.1f} "
                f"first={vals[0]} last={vals[-1]} "
                f"unique={len(set(vals))}"
            )

        summarize("DNE 0x02", dne_02)
        summarize("DNE 0x03", dne_03)

        if dne_02 and dne_03:
            mean_diff = abs((sum(dne_02) / len(dne_02)) - (sum(dne_03) / len(dne_03)))
            print(f"\n  Mean DNE difference: {mean_diff:.2f}")
            if mean_diff < 2.0:
                print("  → DNE values are NEARLY IDENTICAL between modes")
            else:
                print("  → DNE values DIFFER — different baseline filter likely")

        # ── FLASH STORAGE VERDICT ────────────────────────────────────
        print_header("FLASH STORAGE VERDICT")
        print(f"  Baseline buffer:     {buf_bl_len:6d} bytes")
        print(f"  After 0x02 stream:   {buf_mid_len:6d} bytes")
        print(f"  After 0x03 stream:   {buf_final_len:6d} bytes")

        if buf_final_len > buf_mid_len:
            growth = buf_final_len - buf_mid_len
            print(f"  → BUFFER GREW by {growth} bytes during 0x03 phase!")
            print(f"  → MODE 0x03 WRITES TO INTERNAL FLASH!")
        elif buf_final_len > buf_bl_len:
            print(
                f"  → Buffer has data but grew during 0x02 phase "
                f"(or was pre-existing)"
            )
        else:
            print(f"  → Buffer empty across all phases.")
            print(f"  → Neither mode writes to flash (or flash is auto-cleared).")

        # ── Register change summary ──────────────────────────────────
        print_header("REGISTER STATE TRANSITIONS")
        for label in regs_bl:
            vals = []
            for phase, regs in [
                ("baseline", regs_bl),
                ("after_0x02", regs_after_02),
                ("after_0x03", regs_after_03),
                ("final", regs_final),
            ]:
                vals.append(f"{phase}={regs.get(label, '?')}")
            print(f"  {label}: {' → '.join(vals)}")

        # ── Write CSV ────────────────────────────────────────────────
        print_header(f"WRITING CSV: {csv_path}")
        if all_packets:
            # Collect all possible field names across all entries
            fieldnames = [
                "unix",
                "mode",
                "stream",
                "hex",
                "len",
                "timestamp_ms",
                "instant",
                "dne",
                "header",
                "mystery_u16_0",
                "mystery_u16_1",
                "mystery_u32",
                "state",
                "imu_clock",
                "imu_ctx",
            ]
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(all_packets)
            print(f"[OK] {len(all_packets)} packets written to {csv_path}")
        else:
            print("[WARN] No packets captured!")

        print_header("PROBE COMPLETE")
        print(f"CSV: {csv_path}")
        print(f"Buffers: {buf_dir}")
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

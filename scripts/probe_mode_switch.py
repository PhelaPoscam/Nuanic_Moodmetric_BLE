#!/usr/bin/env python3
"""Systematic Live Mode-Switch & Button Trigger Probe (Long Window).

Designed specifically to respect the ring's 1-2 minute internal algorithm reset and
calibration period after a mode switch command is written.

Instead of rapid-fire writing, this script lets you test one specific hex pattern
(e.g. 0x01, 0x02, 0x03) and monitor telemetry continuously over a 60-120s window
to observe the full reset, baseline recalibration, and stream transition.
"""

import argparse
import asyncio
import platform
import struct
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

try:
    from nuanic_ring.connector import NuanicConnector
    from nuanic_ring.discover_services import (
        WRITE_ONLY_CHARS,
        WRITE_READ_CHARS,
        NotifyStats,
        print_header,
        resolve_profile,
    )
    from nuanic_ring.moodmetric_parser import decode_moodmetric_payload, summarize_decoded_payload
    from nuanic_ring.ring_profiles import (
        MOODMETRIC_PROFILE,
        NUANIC_PROFILE,
        UNKNOWN_PROFILE,
        notify_uuids_for_profile,
    )
except ModuleNotFoundError:
    print(
        "[ERROR] Could not import 'nuanic_ring'. "
        "Install the project first: pip install -e .[dev]"
    )
    raise SystemExit(1)

# Target probe registers
PROBE_REGISTERS = {
    "config3": "3cce21a7-e602-4e02-8c52-1e0366c1c846",  # CONFIG_3_STORAGE_FORMAT (Read/Write)
    "write1": "2175c13f-60e4-4de5-80af-0d06f1b54880",   # WRITE_1_COMMAND_TRIGGER (Write-Only)
    "sample_rate": "516b0fb6-d861-4619-9dd0-0105e8b85128", # CONFIG_1_SAMPLE_RATE (Read/Write)
}

# Candidate hex button / mode switch patterns
KNOWN_PATTERNS = {
    "0x00": b"\x00",
    "0x01": b"\x01",
    "0x02": b"\x02",
    "0x03": b"\x03",
    "0x04": b"\x04",
    "0x0100": b"\x01\x00",
    "0x0101": b"\x01\x01",
    "0xff": b"\xff",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test a single hex 'button' switch and monitor the ring's 1-2 min reset cycle."
    )
    parser.add_argument(
        "--ring-addr",
        help="Target BLE MAC address (if omitted, uses cached or scans).",
        default=None,
    )
    parser.add_argument(
        "--register",
        choices=list(PROBE_REGISTERS.keys()) + ["3cce21a7-e602-4e02-8c52-1e0366c1c846", "2175c13f-60e4-4de5-80af-0d06f1b54880"],
        default="config3",
        help="Register to write: 'config3' (STORAGE_FORMAT 3cce...), 'write1' (2175...), or full UUID.",
    )
    parser.add_argument(
        "--pattern",
        default="0x02",
        help="Hex pattern to write: e.g. '0x01', '0x02', '0x03', '0xFF' (default: 0x02).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=90.0,
        help="Seconds to observe telemetry after the write to allow full algorithm reset (default: 90.0s).",
    )
    parser.add_argument(
        "--report-interval",
        type=float,
        default=10.0,
        help="Print status summary every N seconds during observation (default: 10.0s).",
    )
    return parser.parse_args()


def parse_hex_pattern(pat_str: str) -> bytes:
    clean = pat_str.lower().strip()
    if clean in KNOWN_PATTERNS:
        return KNOWN_PATTERNS[clean]
    if clean.startswith("0x"):
        clean = clean[2:]
    try:
        return bytes.fromhex(clean)
    except ValueError:
        print(f"[ERROR] Invalid hex pattern: {pat_str}. Falling back to 0x02.")
        return b"\x02"


async def read_register_states(client) -> Dict[str, str]:
    """Read back all readable configuration registers to observe state changes."""
    states = {}
    for uuid, label in WRITE_READ_CHARS.items():
        try:
            val = await client.read_gatt_char(uuid)
            states[label] = bytes(val).hex()
        except Exception as exc:
            states[label] = f"ERR({exc})"
    return states


async def probe_single_command(client, reg_uuid: str, pat_bytes: bytes, duration: float, report_interval: float):
    print_header("1. STREAM SUBSCRIPTION & BASELINE")
    resolved_profile = resolve_profile(client, "auto")
    print(f"[PROFILE] Active Profile: {resolved_profile.upper()}")

    notify_uuids = notify_uuids_for_profile(resolved_profile)
    stream_stats: Dict[str, NotifyStats] = {u.lower(): NotifyStats() for u in notify_uuids}
    window_stats: Dict[str, int] = {u.lower(): 0 for u in notify_uuids}

    def make_cb(uuid_str: str):
        def cb(_sender, data):
            payload = bytes(data)
            key = uuid_str.lower()
            stream_stats[key].add(payload)
            window_stats[key] += 1
        return cb

    active_uuids = []
    for uuid in notify_uuids:
        try:
            await client.start_notify(uuid, make_cb(uuid))
            active_uuids.append(uuid)
            print(f"  [SUB OK] {uuid}")
        except Exception as exc:
            print(f"  [SUB FAIL] {uuid}: {exc}")

    if not active_uuids:
        print("[FAIL] Could not subscribe to any notifications. Exiting.")
        return

    print("\n[INIT] Recording 5.0s baseline before writing command...")
    await asyncio.sleep(5.0)

    initial_regs = await read_register_states(client)
    print(f"[BASELINE REGISTERS] {initial_regs}")
    for u in active_uuids:
        st = stream_stats[u.lower()]
        print(f"  Baseline {u[:8]}... : {st.count} pkts ({st.freq_hz():.2f} Hz)")

    # Execute Single Mode-Switch Write
    print_header(f"2. EXECUTING MODE SWITCH\nTarget Register: {reg_uuid}\nCommand Pattern: {pat_bytes.hex().upper()}")
    
    # Clear stats for post-write window
    for u in stream_stats:
        stream_stats[u] = NotifyStats()
        window_stats[u] = 0

    write_start_time = time.time()
    try:
        await client.write_gatt_char(reg_uuid, pat_bytes)
        print(f"[WRITE OK] Successfully sent command {pat_bytes.hex().upper()} at t=0.0s")
    except Exception as exc:
        print(f"[WRITE FAIL] Failed to write {pat_bytes.hex().upper()}: {exc}")
        return

    post_write_regs = await read_register_states(client)
    print(f"[REGISTERS AFTER WRITE] {post_write_regs}")

    # Long Observation Window for Algorithm Reset & Calibration
    print_header(f"3. OBSERVING RING RESET & STREAM BEHAVIOR ({duration}s)")
    print(f"Monitoring every {report_interval}s to observe calibration stabilization and stream activation...\n")

    elapsed = 0.0
    while elapsed < duration:
        sleep_chunk = min(report_interval, duration - elapsed)
        await asyncio.sleep(sleep_chunk)
        elapsed = time.time() - write_start_time

        print(f"--- [t = {elapsed:5.1f}s after switch] ---")
        for u in active_uuids:
            st = stream_stats[u.lower()]
            pkts_in_win = window_stats[u.lower()]
            win_hz = pkts_in_win / sleep_chunk
            window_stats[u.lower()] = 0  # reset chunk counter

            if st.count > 0:
                print(f"  * {u[:8]}... | Rate: {win_hz:5.2f} Hz (Total: {st.count:4d})", end="")
                
                # Detailed decoding of latest packet
                last_pkt = st.last_packet
                if u.lower() == "d306262b-c8c9-4c4b-9050-3a41dea706e5" and last_pkt and len(last_pkt) == 16:
                    ctx = struct.unpack("<I", last_pkt[4:8])[0]
                    eda = struct.unpack("<I", last_pkt[8:12])[0]
                    dne = struct.unpack("<I", last_pkt[12:16])[0]
                    print(f" => [D306] Ctx:{ctx} | Raw EDA:{eda} | DNE(Algo):{dne}")
                elif u.lower() == "42dcb71b-1817-43bd-8ea3-7272780a1c9f" and last_pkt:
                    print(f" => [!!! ALGO/DNE NOTIFY FIRED !!!] Hex: {last_pkt.hex()}")
                elif resolved_profile == MOODMETRIC_PROFILE and last_pkt:
                    decoded = decode_moodmetric_payload(u, last_pkt)
                    print(f" => [{summarize_decoded_payload(decoded)}]")
                else:
                    print(f" => Hex: {last_pkt.hex()[:24]}")
            else:
                if u.lower() == "42dcb71b-1817-43bd-8ea3-7272780a1c9f":
                    print(f"  * {u[:8]}... | SILENT (Waiting for 1-minute Algo historical summary...)")
                else:
                    print(f"  * {u[:8]}... | SILENT (0 pkts)")

        # Read registers periodically during reset to see if flags change
        if int(elapsed) % int(report_interval * 2) == 0:
            cur_regs = await read_register_states(client)
            print(f"  [Register Status @ t={elapsed:.1f}s] {cur_regs}")
        print()

    print_header("4. FINAL SUMMARY")
    final_regs = await read_register_states(client)
    print(f"[FINAL REGISTERS] {final_regs}")
    for u in active_uuids:
        st = stream_stats[u.lower()]
        print(f"  {u}: Total Packets={st.count} | Avg Rate={st.freq_hz():.2f} Hz")

    # Cleanup
    for u in active_uuids:
        try:
            await client.stop_notify(u)
        except Exception:
            pass
    print("\n[DONE] Probe completed cleanly.")


async def main() -> int:
    args = parse_args()
    
    reg_uuid = PROBE_REGISTERS.get(args.register.lower(), args.register)
    pat_bytes = parse_hex_pattern(args.pattern)

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

        print(f"\n[CONNECTED] Ring Address: {connector.target_address}")
        await probe_single_command(
            connector.client,
            reg_uuid=reg_uuid,
            pat_bytes=pat_bytes,
            duration=args.duration,
            report_interval=args.report_interval,
        )
        return 0
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted by user.")
        return 1
    finally:
        await connector.disconnect()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted by user.")
        raise SystemExit(1)

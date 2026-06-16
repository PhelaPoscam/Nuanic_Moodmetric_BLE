#!/usr/bin/env python3
"""Unified ring diagnostics CLI.

This script consolidates exploratory reverse-engineering tools into one entrypoint:
- Service/characteristic discovery
- Notify packet profiling (size and rate)
- Optional write-probe on config characteristics
- Optional buffer inspection
"""

import argparse
import asyncio
import platform
import struct
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, Optional

try:
    from nuanic_ring.connector import NuanicConnector
    from nuanic_ring.ring_profiles import (
        MOODMETRIC_PROFILE,
        NUANIC_PROFILE,
        UNKNOWN_PROFILE,
        detect_ring_profile_from_service_uuids,
        notify_uuids_for_profile,
    )
except ModuleNotFoundError:
    print(
        "[ERROR] Could not import 'nuanic_ring'. "
        "Install the project first: pip install -e .[dev]"
    )
    raise SystemExit(1)

SERVICE_UUID = "5491faaf-b0c2-4167-8f3d-bc6b31db69e7"
BUFFER_CHAR = "7c3b82e7-22b7-4cb6-8458-ba325edf6ede"
MYSTERY_NOTIFY = "42dcb71b-1817-43bd-8ea3-7272780a1c9f"

WRITE_ONLY_CHARS = {
    "2175c13f-60e4-4de5-80af-0d06f1b54880": "WRITE_1",
}

WRITE_READ_CHARS = {
    "516b0fb6-d861-4619-9dd0-0105e8b85128": "CONFIG_1",
    "dc9c31a7-fbd3-467a-8777-10900c423d3b": "CONFIG_2",
    "3cce21a7-e602-4e02-8c52-1e0366c1c846": "CONFIG_3",
}

WRITE_PATTERNS = {
    "enable_1": b"\x01",
    "enable_1_0": b"\x01\x00",
    "enable_2": b"\x02",
    "enable_all": b"\xff",
    "mode_3": b"\x03",
    "stream_1_1": b"\x01\x01",
    "extended_4": b"\x04",
    "reset_0": b"\x00",
}


class NotifyStats:
    def __init__(self):
        self.count = 0
        self.first_ts = None
        self.last_ts = None
        self.size_dist = defaultdict(int)
        self.first_packet = None
        self.last_packet = None

    def add(self, data: bytes):
        now = time.time()
        self.count += 1
        if self.first_ts is None:
            self.first_ts = now
            self.first_packet = data
        self.last_ts = now
        self.last_packet = data
        self.size_dist[len(data)] += 1

    def freq_hz(self) -> float:
        if not self.first_ts or not self.last_ts or self.last_ts <= self.first_ts:
            return 0.0
        return self.count / (self.last_ts - self.first_ts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ring diagnostics: discover, profile notify streams, write-probe, and inspect buffer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --ring-addr 56:C2:72:F2:07:04
  %(prog)s --ring-addr 56:C2:72:F2:07:04 --profile-seconds 20
  %(prog)s --ring-addr 56:C2:72:F2:07:04 --write-probe --buffer-poll 5
  %(prog)s --ring-addr 56:C2:72:F2:07:04 --no-profile --buffer-only
""",
    )
    parser.add_argument(
        "legacy_ring_addr",
        nargs="?",
        help="Optional BLE address (legacy positional argument).",
    )
    parser.add_argument(
        "--ring-addr",
        default=None,
        help="BLE address of ring. If omitted, connector selection is used.",
    )
    parser.add_argument(
        "--profile-seconds",
        type=int,
        default=15,
        help="Duration for notify profiling (default: 15).",
    )
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="Skip notify profiling.",
    )
    parser.add_argument(
        "--write-probe",
        action="store_true",
        help="Run write probes against known config characteristics.",
    )
    parser.add_argument(
        "--buffer-poll",
        type=int,
        default=1,
        help="Read buffer characteristic N times (default: 1).",
    )
    parser.add_argument(
        "--buffer-interval",
        type=float,
        default=2.0,
        help="Seconds between buffer reads when --buffer-poll > 1 (default: 2.0).",
    )
    parser.add_argument(
        "--buffer-only",
        action="store_true",
        help="Only inspect buffer (skip discovery and profile).",
    )
    parser.add_argument(
        "--only-stream-chars",
        action="store_true",
        help=(
            "In discovery output, show only characteristics with "
            "Notify/Indicate properties"
        ),
    )
    parser.add_argument(
        "--subscribe-core-streams",
        action="store_true",
        help=(
            "Subscribe to the 4 proprietary notify streams and print "
            "timestamp/uuid/len/hex continuously"
        ),
    )
    parser.add_argument(
        "--listen-seconds",
        type=int,
        default=None,
        help=(
            "Optional duration for --subscribe-core-streams. "
            "Default: run until Ctrl+C/disconnect"
        ),
    )
    parser.add_argument(
        "--ring-profile",
        choices=["auto", NUANIC_PROFILE, MOODMETRIC_PROFILE],
        default="auto",
        help=(
            "Profile selection for profile-specific operations. "
            "Default: auto-detect from discovered services."
        ),
    )
    if platform.system() == "Windows":
        parser.add_argument(
            "--keep-paired-on-exit",
            action="store_true",
            help="[Windows only] Prevents un-pairing from the device on exit. By default, un-pairing is done to ensure clean reconnections.",
        )
    else:
        parser.add_argument(
            "--unpair-on-exit",
            action="store_true",
            help="Force OS un-pair on exit. Can help with connection issues on some systems.",
        )
    return parser.parse_args()


def detect_profile_from_client(client) -> str:
    service_uuids = [service.uuid for service in client.services]
    return detect_ring_profile_from_service_uuids(service_uuids)


def resolve_profile(client, requested_profile: str) -> str:
    if requested_profile != "auto":
        return requested_profile
    return detect_profile_from_client(client)


def pick_ring_addr(args: argparse.Namespace) -> Optional[str]:
    return args.ring_addr or args.legacy_ring_addr


def print_header(title: str):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def iter_service_chars(client):
    for service in client.services:
        if service.uuid.lower() == SERVICE_UUID.lower():
            for char in service.characteristics:
                yield char


def iter_all_chars(client):
    for service in client.services:
        for char in service.characteristics:
            yield service, char


def summarize_i16(packet: bytes) -> str:
    if len(packet) < 2:
        return "n/a"
    vals = []
    for i in range(0, len(packet) - 1, 2):
        vals.append(struct.unpack("<h", packet[i : i + 2])[0])
    if not vals:
        return "n/a"
    return f"min={min(vals)}, max={max(vals)}, avg={sum(vals)/len(vals):.1f}, count={len(vals)}"


def is_stream_characteristic(char) -> bool:
    props = {p.lower() for p in (char.properties or [])}
    return "notify" in props or "indicate" in props


async def discover_chars(client, only_stream_chars: bool = False):
    print_header("STEP 1: DISCOVERY")
    services = list(client.services)
    all_chars = list(iter_all_chars(client))
    target_chars = list(iter_service_chars(client))

    print(f"Discovered services: {len(services)}")
    print(f"Discovered characteristics: {len(all_chars)}")
    print(f"Target service UUID: {SERVICE_UUID}\n")

    mode_label = "Notify/Indicate only" if only_stream_chars else "All"
    print(f"Discovery mode: {mode_label}\n")

    for s_idx, service in enumerate(services, 1):
        svc_desc = service.description or "n/a"
        if only_stream_chars:
            visible_chars = [
                c for c in service.characteristics if is_stream_characteristic(c)
            ]
        else:
            visible_chars = list(service.characteristics)

        if not visible_chars:
            continue

        print(f"[SVC {s_idx:02d}] {service.uuid}")
        print(f"          Description: {svc_desc}")
        print(f"          Characteristics: {len(visible_chars)}")

        for c_idx, char in enumerate(visible_chars, 1):
            props = ", ".join(sorted(char.properties)) if char.properties else "n/a"
            char_desc = char.description or "n/a"
            print(f"            [{c_idx:02d}] {char.uuid}")
            print(f"                 Properties: {props}")
            print(f"                 Description: {char_desc}")

        print()

    if not target_chars:
        print(f"[WARN] Target service {SERVICE_UUID} not found in this session")

    return {
        "all_chars": [char for _service, char in all_chars],
        "target_chars": target_chars,
    }


async def read_non_notify(client, chars):
    print_header("STEP 2: READ SNAPSHOT")
    for char in chars:
        props = set(char.properties)
        if "read" in props and "notify" not in props:
            try:
                value = await client.read_gatt_char(char.uuid)
                preview = bytes(value).hex()[:64]
                print(f"[READ] {char.uuid} | {len(value)} bytes | {preview}...")
            except Exception as exc:
                print(f"[READ] {char.uuid} | FAILED: {exc}")


async def profile_notify(client, chars, seconds: int):
    print_header("STEP 3: NOTIFY PROFILE")
    stats: Dict[str, NotifyStats] = {}
    notify_chars = [c for c in chars if "notify" in c.properties]

    for char in notify_chars:
        key = char.uuid.lower()
        stats[key] = NotifyStats()

        def make_cb(k):
            def cb(_sender, data):
                stats[k].add(bytes(data))

            return cb

        await client.start_notify(char.uuid, make_cb(key))
        print(f"[SUB] {char.uuid}")

    print(f"\nListening for {seconds}s... move/interact with the ring.\n")
    await asyncio.sleep(max(1, seconds))

    for char in notify_chars:
        try:
            await client.stop_notify(char.uuid)
        except Exception:
            pass

    print_header("STEP 4: PROFILE RESULTS")
    for char in notify_chars:
        key = char.uuid.lower()
        st = stats[key]
        print(f"[NOTIFY] {char.uuid}")
        print(f"  packets: {st.count}")
        print(f"  rate: {st.freq_hz():.2f} Hz")
        print(f"  sizes: {dict(sorted(st.size_dist.items())) if st.size_dist else {}}")
        if st.first_packet is not None:
            print(f"  first: {st.first_packet.hex()[:64]}...")
            print(f"  last:  {st.last_packet.hex()[:64]}...")
            print(f"  int16 summary: {summarize_i16(st.first_packet)}")
        if key == MYSTERY_NOTIFY.lower() and st.count == 0:
            print("  note: mystery notify remained silent in this run")


async def run_write_probe(client):
    print_header("STEP 4: WRITE PROBE")
    for name, payload in WRITE_PATTERNS.items():
        print(f"\n[CMD] {name}: {payload.hex()}")
        for uuid, label in WRITE_READ_CHARS.items():
            try:
                await client.write_gatt_char(uuid, payload)
                try:
                    echoed = await client.read_gatt_char(uuid)
                    print(
                        f"  [OK] {label} ({uuid[:8]}...) echo={bytes(echoed).hex()[:32]}"
                    )
                except Exception:
                    print(f"  [OK] {label} ({uuid[:8]}...) write only in this session")
            except Exception as exc:
                print(f"  [FAIL] {label} ({uuid[:8]}...): {exc}")

        for uuid, label in WRITE_ONLY_CHARS.items():
            try:
                await client.write_gatt_char(uuid, payload)
                print(f"  [OK] {label} ({uuid[:8]}...)")
            except Exception as exc:
                print(f"  [FAIL] {label} ({uuid[:8]}...): {exc}")


async def inspect_buffer(client, polls: int, interval: float):
    print_header("STEP 5: BUFFER INSPECTION")
    previous = None
    for idx in range(max(1, polls)):
        try:
            data = bytes(await client.read_gatt_char(BUFFER_CHAR))
            changed = previous is not None and previous != data
            print(
                f"[BUF {idx+1}] {len(data)} bytes | changed={changed} | first32={data.hex()[:64]}..."
            )
            if len(data) >= 2:
                print(
                    f"         int16 summary: {summarize_i16(data[: min(len(data), 120)])}"
                )
            previous = data
        except Exception as exc:
            print(f"[BUF {idx+1}] FAILED: {exc}")

        if idx < polls - 1:
            await asyncio.sleep(max(0.1, interval))


def _format_hex_spaced(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def parse_eda_data(sender, data: bytearray):
    """Parse 16-byte d306 EDA frame and print key fields.

    Layout (little-endian):
    - bytes 0-3: hardware timestamp (uint32)
    - bytes 4-7: static context/session (ignored)
    - bytes 8-11: raw EDA value (uint32)
    - bytes 12-15: signal quality/contact score (uint32, expected 0-100)
    """
    payload = bytes(data)
    ts = datetime.now().isoformat(timespec="milliseconds")
    if len(payload) != 16:
        print(
            f"[{ts}] [EDA STREAM] Invalid payload length: {len(payload)} "
            f"(expected 16)"
        )
        return

    clock = struct.unpack("<I", payload[0:4])[0]
    eda_value = struct.unpack("<I", payload[8:12])[0]
    signal_quality = struct.unpack("<I", payload[12:16])[0]
    print(
        f"[{ts}] [EDA STREAM] Clock: {clock} | EDA Value: {eda_value} | "
        f"Signal Quality: {signal_quality}%"
    )


async def subscribe_core_streams(
    client,
    listen_seconds: int | None = None,
    ring_profile: str = "auto",
):
    """Subscribe to profile-specific notify characteristics.

    Prints one line per notification with timestamp, UUID, payload length,
    and clean uppercase hex bytes.
    """
    print_header("LIVE CORE STREAMS")

    resolved_profile = resolve_profile(client, ring_profile)
    active_profile_label = resolved_profile.upper()

    if resolved_profile == UNKNOWN_PROFILE:
        print(
            "[WARN] Could not auto-detect ring profile from services; "
            "defaulting to NUANIC notify set."
        )
        resolved_profile = NUANIC_PROFILE

    notify_uuids = notify_uuids_for_profile(resolved_profile)
    print(
        f"[PROFILE] Using {active_profile_label} notify set ({len(notify_uuids)} UUIDs)"
    )

    stream_stats: Dict[str, NotifyStats] = {
        uuid.lower(): NotifyStats() for uuid in notify_uuids
    }

    disconnected = asyncio.Event()

    def on_disconnect(_c):
        print("\n[DISCONNECT] Ring disconnected")
        disconnected.set()

    try:
        client.set_disconnected_callback(on_disconnect)
    except Exception:
        # Some backends may not support this callback.
        pass

    active_uuids: list[str] = []

    for uuid in notify_uuids:
        try:

            def make_cb(char_uuid: str):
                def cb(_sender, data):
                    payload = bytes(data)
                    stats_key = char_uuid.lower()
                    stream_stats[stats_key].add(payload)

                    if (
                        resolved_profile == NUANIC_PROFILE
                        and char_uuid.lower() == "d306262b-c8c9-4c4b-9050-3a41dea706e5"
                    ):
                        parse_eda_data(_sender, bytearray(payload))
                        return

                    ts = datetime.now().isoformat(timespec="milliseconds")
                    print(
                        f"[{ts}] uuid={char_uuid} len={len(payload)} "
                        f"hex={_format_hex_spaced(payload)}"
                    )

                return cb

            await client.start_notify(uuid, make_cb(uuid))
            active_uuids.append(uuid)
            print(f"[SUB] {uuid}")
        except Exception as exc:
            print(f"[SUB-FAIL] {uuid} -> {exc}")

    if not active_uuids:
        print("[FAIL] Could not subscribe to any core notify streams")
        return

    print("\n[LISTEN] Streaming started")
    if listen_seconds is None:
        print("[LISTEN] Running until Ctrl+C or disconnect")
    else:
        print(f"[LISTEN] Running for {listen_seconds}s")

    try:
        if listen_seconds is None:
            while not disconnected.is_set():
                await asyncio.sleep(1)
        else:
            end_time = time.time() + max(1, listen_seconds)
            while time.time() < end_time and not disconnected.is_set():
                await asyncio.sleep(1)
    finally:
        for uuid in active_uuids:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass

        print_header("CORE STREAM SUMMARY")
        silent_uuids: list[str] = []
        for uuid in notify_uuids:
            key = uuid.lower()
            stats = stream_stats[key]
            if stats.count == 0:
                silent_uuids.append(uuid)
            print(f"[SUMMARY] {uuid}")
            print(f"  packets: {stats.count}")
            print(f"  rate: {stats.freq_hz():.2f} Hz")
            print(
                f"  sizes: {dict(sorted(stats.size_dist.items())) if stats.size_dist else {}}"
            )

        if silent_uuids:
            print("[WARN] Silent UUIDs in this run:")
            for uuid in silent_uuids:
                print(f"  - {uuid}")


async def main() -> int:
    args = parse_args()
    ring_addr = pick_ring_addr(args)

    unpair_on_exit_flag = False
    if platform.system() == "Windows":
        # On Windows, we unpair by default. The --keep-paired-on-exit flag disables this.
        if not args.keep_paired_on_exit:
            unpair_on_exit_flag = True
    else:
        # On other OSes, we only unpair if requested.
        if "unpair_on_exit" in args and args.unpair_on_exit:
            unpair_on_exit_flag = True

    connector = NuanicConnector(
        target_address=ring_addr,
        unpair_on_disconnect=unpair_on_exit_flag,
        max_connect_attempts=3,
        connect_backoff_seconds=2.0,
        pair_on_connect=True,
    )
    try:
        try:
            if not await connector.connect():
                print("[FAIL] Could not connect to ring")
                return 1

            print(
                f"\n[INIT] Connected to: {connector.target_address or 'selected ring'}"
            )

            client = connector.client
            if args.subscribe_core_streams:
                await subscribe_core_streams(
                    client,
                    listen_seconds=args.listen_seconds,
                    ring_profile=args.ring_profile,
                )
                return 0

            if args.buffer_only:
                available_uuids = {
                    char.uuid.lower() for _service, char in iter_all_chars(client)
                }
                if BUFFER_CHAR.lower() not in available_uuids:
                    print(
                        "[SKIP] Buffer inspection skipped: target buffer characteristic "
                        f"{BUFFER_CHAR} is not exposed by this device profile."
                    )
                    return 0
                await inspect_buffer(
                    client, polls=args.buffer_poll, interval=args.buffer_interval
                )
                return 0

            discovered = await discover_chars(
                client, only_stream_chars=args.only_stream_chars
            )
            all_chars = discovered["all_chars"]
            target_chars = discovered["target_chars"]

            await read_non_notify(client, all_chars)

            if not args.no_profile:
                await profile_notify(client, all_chars, seconds=args.profile_seconds)

            if args.write_probe:
                if target_chars:
                    await run_write_probe(client)
                else:
                    print(
                        f"[SKIP] Write probe skipped because target service {SERVICE_UUID} is missing"
                    )

            if args.buffer_poll > 0:
                if target_chars:
                    await inspect_buffer(
                        client, polls=args.buffer_poll, interval=args.buffer_interval
                    )
                else:
                    print(
                        f"[SKIP] Buffer inspection skipped because target service {SERVICE_UUID} is missing"
                    )

            return 0
        except KeyboardInterrupt:
            print("\n[STOP] Interrupted by user")
            return 1
    finally:
        # Keep disconnect as the final awaited cleanup action before exit.
        await connector.disconnect()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted by user")
        raise SystemExit(1)

"""Ring diagnostics — service discovery, notify profiling, buffer inspection.

Driven by ``nuanic-ring-monitor --discover``; :func:`run_diagnostics` is the
entry point and expects an already-connected BLE client.
"""

import asyncio
import struct
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict

from nuanic_ring.core.profiles import (
    LIVE_DNE_UUID,
    LIVE_EDA_UUID,
    NUANIC_PROFILE,
    NUANIC_SERVICE_UUID,
    STORAGE_UUID,
    UNKNOWN_PROFILE,
    detect_ring_profile_from_service_uuids,
    notify_uuids_for_profile,
)
from nuanic_ring.io.schemas import D306Packet, LiveEdaPacket

SERVICE_UUID = NUANIC_SERVICE_UUID


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
        if key == LIVE_EDA_UUID.lower() and st.count == 0:
            print("  note: algo/dne notify remained silent in this run")


async def inspect_buffer(client, polls: int, interval: float):
    print_header("STEP 5: BUFFER INSPECTION")
    previous = None
    for idx in range(max(1, polls)):
        try:
            data = bytes(await client.read_gatt_char(STORAGE_UUID))
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


def _decode_nuanic_stream(char_uuid: str, payload: bytes) -> str | None:
    """Human-readable line for the two decodable Nuanic streams, else ``None``."""
    uuid = char_uuid.lower()
    if uuid == LIVE_DNE_UUID.lower():
        packet = D306Packet.from_bytes(payload)
        if packet is None:
            return f"[LIVE DNE] bad payload length: {len(payload)} (expected 16)"
        return (
            f"[LIVE DNE] Timestamp: {packet.timestamp_ms} ms | "
            f"Instant Indicator: {packet.instant} | DNE: {packet.dne}"
        )

    if uuid == LIVE_EDA_UUID.lower():
        eda = LiveEdaPacket.from_bytes(payload)
        if eda is None:
            return f"[LIVE EDA] bad payload length: {len(payload)} (expected 14)"
        ohms = eda.eda_raw_ohms
        cond_us = (1_000_000.0 / ohms) if ohms > 0 else 0.0
        return (
            f"[LIVE EDA] Boot: {eda.boot_count} | "
            f"Timestamp: {eda.timestamp_ms} ms | "
            f"Resistance: {ohms / 1000.0:.3f} kOhm | Conductance: {cond_us:.3f} uS"
        )

    return None


def _print_stream_summary(
    notify_uuids: list, stream_stats: Dict[str, NotifyStats]
) -> None:
    """Print per-UUID packet counts, rates, and size distributions."""
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

    if ring_profile != "auto":
        resolved_profile = ring_profile
    else:
        resolved_profile = detect_ring_profile_from_service_uuids(
            [service.uuid for service in client.services]
        )
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

    active_uuids: list[str] = []

    for uuid in notify_uuids:
        try:

            def make_cb(char_uuid: str):
                def cb(_sender, data):
                    payload = bytes(data)
                    stream_stats[char_uuid.lower()].add(payload)

                    ts = datetime.now().isoformat(timespec="milliseconds")
                    if resolved_profile == NUANIC_PROFILE:
                        decoded = _decode_nuanic_stream(char_uuid, payload)
                        if decoded:
                            print(f"[{ts}] {decoded}")
                            return

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
        end_time = (
            None if listen_seconds is None else time.time() + max(1, listen_seconds)
        )
        while client.is_connected and (end_time is None or time.time() < end_time):
            await asyncio.sleep(1)
        if not client.is_connected:
            print("\n[DISCONNECT] Ring disconnected")
    finally:
        for uuid in active_uuids:
            try:
                await client.stop_notify(uuid)
            except Exception:
                pass

        _print_stream_summary(notify_uuids, stream_stats)


async def run_diagnostics(
    client,
    *,
    subscribe_streams: bool = False,
    listen_seconds: int | None = None,
    ring_profile: str = "auto",
    buffer_only: bool = False,
    buffer_poll: int = 1,
    buffer_interval: float = 2.0,
    only_stream_chars: bool = False,
    no_profile: bool = False,
    profile_seconds: int = 15,
) -> int:
    """Run the full diagnostic suite against a connected BLE client.

    Callers that don't need every knob can rely on the defaults
    (1 buffer poll, 15 s profile).
    """
    if subscribe_streams:
        await subscribe_core_streams(
            client,
            listen_seconds=listen_seconds,
            ring_profile=ring_profile,
        )
        return 0

    if buffer_only:
        available_uuids = {
            char.uuid.lower() for _service, char in iter_all_chars(client)
        }
        if STORAGE_UUID.lower() not in available_uuids:
            print(
                "[SKIP] Buffer inspection skipped: target buffer characteristic "
                f"{STORAGE_UUID} is not exposed by this device profile."
            )
            return 0
        await inspect_buffer(client, polls=buffer_poll, interval=buffer_interval)
        return 0

    discovered = await discover_chars(client, only_stream_chars=only_stream_chars)
    all_chars = discovered["all_chars"]
    target_chars = discovered["target_chars"]

    await read_non_notify(client, all_chars)

    if not no_profile:
        await profile_notify(client, all_chars, seconds=profile_seconds)

    if buffer_poll > 0:
        if target_chars:
            await inspect_buffer(client, polls=buffer_poll, interval=buffer_interval)
        else:
            print(
                f"[SKIP] Buffer inspection skipped because target service "
                f"{SERVICE_UUID} is missing"
            )

    return 0

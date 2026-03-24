"""Real-time monitor for Nuanic ring streams (d306 EDA+DNE + 468f IMU batch)."""

import asyncio
import csv
import json
import math
import os
import struct
from collections import deque
from datetime import datetime
from pathlib import Path

from .connector import NuanicConnector
from .ring_profiles import (
    MOODMETRIC_PROFILE,
    NUANIC_PROFILE,
    UNKNOWN_PROFILE,
    detect_ring_profile_from_service_uuids,
    notify_uuids_for_profile,
)
from .moodmetric_parser import decode_moodmetric_payload, summarize_decoded_payload


def convert_eda(raw_value: int):
    """Convert raw EDA integer into resistance (kOhm) and conductance (uS)."""
    ADC_MULTIPLIER = 1.0
    resistance_kohm = (raw_value * ADC_MULTIPLIER) / 1000.0
    conductance_us = (1000.0 / resistance_kohm) if resistance_kohm > 0 else 0.0
    return round(resistance_kohm, 4), round(conductance_us, 4)


class NuanicMonitor:
    """Single-entry monitor for real-time display and CSV logging."""

    def __init__(
        self,
        log_dir: str = "data/nuanic_logs",
        imu_refresh_packets: int = 5,
        clear_console: bool = True,
        enable_logging: bool = True,
        calibration_seconds: int = 120,
    ):
        self.log_dir = Path(log_dir)
        self.enable_logging = enable_logging
        if self.enable_logging:
            self.log_dir.mkdir(parents=True, exist_ok=True)

        self.connector = NuanicConnector()  # Ring selection happens at connection time
        self.imu_refresh_packets = max(1, imu_refresh_packets)
        self.clear_console = clear_console

        self.start_time = None
        self.d306_count = 0
        self.imu_batch_count = 0

        self.current_stress = None
        self.current_eda_raw = None
        self.current_resistance_kohm = None
        self.current_conductance_us = None
        self.current_d306_clock = None
        self.current_dne_stress_index = None

        self.log_file = None

        self.d306_buffer = deque(maxlen=10)
        self.imu_batch_buffer = deque(maxlen=5)
        self.raw_eda_buffer = deque(maxlen=10)
        self.current_d306_context = None
        self.current_3c18_state_code = None
        self.current_3c18_state_name = "unknown"
        self.current_live_eda_hex = None
        self.current_live_eda_len = 0
        self.live_eda_count = 0
        self.state_count = 0
        self._detected_profile = UNKNOWN_PROFILE

    def _parse_d306_packet(self, data):
        """Parse d306 16-byte frame (little-endian uint32 chunks)."""
        if len(data) != 16:
            return None

        return {
            "clock": struct.unpack("<I", data[0:4])[0],
            "context": struct.unpack("<I", data[4:8])[0],
            "eda_value": struct.unpack("<I", data[8:12])[0],
            # Current hypothesis: trailing field is DNE/MM-like stress index 0..100.
            "dne_stress_index": struct.unpack("<I", data[12:16])[0],
        }

    def _parse_468f_imu_batch(self, data):
        """Parse 468f 92-byte payload as 14 batched XYZ IMU frames.

        Layout (little-endian):
        - bytes 0-3: hardware timestamp
        - bytes 4-7: context/session id
        - bytes 8-91: 14 samples x (x:int16, y:int16, z:int16)
        """
        if len(data) != 92:
            return None

        clock = struct.unpack("<I", data[0:4])[0]
        context = struct.unpack("<I", data[4:8])[0]

        samples = []
        offset = 8
        for _ in range(14):
            x, y, z = struct.unpack_from("<hhh", data, offset)
            samples.append((x, y, z))
            offset += 6

        magnitudes = [math.sqrt((x * x) + (y * y) + (z * z)) for x, y, z in samples]
        motion_intensity = sum(magnitudes) / len(magnitudes)

        return {
            "clock": clock,
            "context": context,
            "samples": samples,
            "first_x": samples[0][0],
            "first_y": samples[0][1],
            "first_z": samples[0][2],
            "motion_intensity": motion_intensity,
        }

    def parse_stress_packet(self, data):
        """Parse stress packet; backward-compatible API."""
        if len(data) < 15:
            return None

        stress_raw = data[14]
        stress_percent = (stress_raw / 255) * 100
        eda_raw = data[15:] if len(data) > 15 else bytes()

        return {
            "timestamp": datetime.now(),
            "stress_raw": stress_raw,
            "stress_percent": stress_percent,
            "eda_raw": eda_raw.hex(),
            "full_data": data.hex(),
        }

    async def check_ring_mac_address(self, num_scans: int = 5):
        """Check if ring(s) have dynamic or static MAC addresses.

        Useful for diagnosing connection issues.
        """
        result = await self.connector.check_mac_address_dynamic(num_scans=num_scans)
        return result

    def _elapsed_seconds(self) -> float:
        if not self.start_time:
            return 0.0
        return max(0.001, (datetime.now() - self.start_time).total_seconds())

    def _create_log_files(self):
        if not self.enable_logging:
            self.log_file = None
            return

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        self.log_file = self.log_dir / f"nuanic_{timestamp}.csv"
        with open(self.log_file, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(
                [
                    "timestamp",
                    "elapsed_ms",
                    "data_type",
                    "EDA_Raw_Value",
                    "Stress_Index",
                    "Skin_Resistance_kOhm",
                    "Skin_Conductance_uS",
                    "D306_Clock",
                    "D306_Context",
                    "IMU_Batch_Clock",
                    "IMU_Batch_Context",
                    "IMU_X0",
                    "IMU_Y0",
                    "IMU_Z0",
                    "IMU_Motion_Intensity",
                    "State_Code",
                    "payload_hex",
                    "full_packet_hex",
                    "decoded_fields",
                ]
            )

        print(f"[LOG] Created: {self.log_file.name}\n")

    def _moodmetric_notify_callback_factory(self, uuid: str, packet_counts: dict):
        def _cb(_sender, data):
            ts = datetime.now().isoformat(timespec="milliseconds")
            elapsed_ms = int(self._elapsed_seconds() * 1000)
            payload_hex = bytes(data).hex()
            packet_counts[uuid] = packet_counts.get(uuid, 0) + 1
            decoded = decode_moodmetric_payload(uuid, bytes(data))
            decoded_summary = summarize_decoded_payload(decoded)
            print(
                f"[{ts}] [MM] uuid={uuid} len={len(data)} hex={payload_hex} | {decoded_summary}"
            )

            if self.enable_logging and self.log_file:
                with open(self.log_file, "a", newline="", encoding="utf-8") as file:
                    writer = csv.writer(file)
                    writer.writerow(
                        [
                            ts,
                            elapsed_ms,
                            f"MM_NOTIFY_{uuid[:8]}",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            payload_hex,
                            payload_hex,
                            json.dumps(decoded, sort_keys=True),
                        ]
                    )

        return _cb

    async def _run_moodmetric_monitor(self, duration_seconds=None):
        uuids = notify_uuids_for_profile(MOODMETRIC_PROFILE)
        if not uuids:
            print("[FAIL] Moodmetric notify UUID set is empty")
            return False

        subscribed = []
        packet_counts = {uuid: 0 for uuid in uuids}
        for uuid in uuids:
            try:
                await self.connector.client.start_notify(
                    uuid, self._moodmetric_notify_callback_factory(uuid, packet_counts)
                )
                subscribed.append(uuid)
                print(f"[SUB-MM] {uuid}")
            except Exception as e:
                print(f"[SUB-MM-FAIL] {uuid}: {e}")

        if not subscribed:
            print("[FAIL] Could not subscribe to any Moodmetric notify streams")
            return False

        print("[OK] Moodmetric monitor started")
        print("[INFO] Generic payload capture mode (UUID, length, raw hex)")

        try:
            if duration_seconds is None:
                last_total = 0
                while True:
                    await asyncio.sleep(1)
                    elapsed = int(self._elapsed_seconds())
                    if elapsed > 0 and elapsed % 5 == 0:
                        total = sum(packet_counts.values())
                        if total != last_total:
                            print(
                                f"[MM-STATS] total packets={total} | per UUID: {packet_counts}"
                            )
                            last_total = total
                        elif total == 0 and elapsed >= 10:
                            print(
                                "[MM-WARN] Connected and subscribed, but no notify packets yet. "
                                "Ensure ring is worn/active and keep capture running longer."
                            )
            else:
                await asyncio.sleep(duration_seconds)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n[STOP] Stopping Moodmetric capture...")
        finally:
            for uuid in subscribed:
                try:
                    await self.connector.client.stop_notify(uuid)
                except Exception:
                    pass

        total = sum(packet_counts.values())
        print(f"[MM-SUMMARY] total packets={total}")
        print(f"[MM-SUMMARY] per UUID={packet_counts}")
        if total == 0:
            print(
                "[MM-SUMMARY] No notify packets captured during this session; "
                "subscriptions are active but stream appears idle/gated."
            )

        return True

    def _imu_callback(self, sender, data):
        if len(data) != 16:
            return

        timestamp = datetime.now().isoformat()
        elapsed_ms = int(self._elapsed_seconds() * 1000)

        parsed = self._parse_d306_packet(data)
        if not parsed:
            return

        clock = parsed["clock"]
        context = parsed["context"]
        eda_value = parsed["eda_value"]
        dne_stress_index = parsed["dne_stress_index"]
        resistance_kohm, conductance_us = convert_eda(eda_value)
        full_hex = data.hex()

        if self.enable_logging and self.log_file:
            with open(self.log_file, "a", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(
                    [
                        timestamp,
                        elapsed_ms,
                        "D306_EDA",
                        eda_value,
                        dne_stress_index,
                        f"{resistance_kohm:.4f}",
                        f"{conductance_us:.4f}",
                        clock,
                        context,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        full_hex,
                        "",
                    ]
                )

        self.d306_count += 1
        self.current_d306_context = context
        self.current_d306_clock = clock
        self.current_dne_stress_index = dne_stress_index
        self.current_stress = float(dne_stress_index)
        self.current_eda_raw = str(eda_value)
        self.current_resistance_kohm = resistance_kohm
        self.current_conductance_us = conductance_us

        self.d306_buffer.append(
            {
                "count": self.d306_count,
                "clock": clock,
                "context": context,
                "eda_value": eda_value,
                "dne_stress_index": dne_stress_index,
            }
        )

        if self.d306_count % self.imu_refresh_packets == 0:
            self._update_display()

    def _raw_eda_callback(self, sender, data):
        """Callback for 3c180fcc state/on-finger stream."""
        timestamp = datetime.now().isoformat()
        elapsed_ms = int(self._elapsed_seconds() * 1000)
        raw_hex = data.hex()
        state_code = data[0] if len(data) >= 1 else None
        state_name_map = {
            0x01: "idle/off-finger",
            0x02: "active/on-finger",
            0x03: "transient/poll",
        }
        state_name = state_name_map.get(state_code, "unknown")

        self.current_3c18_state_code = state_code
        self.current_3c18_state_name = state_name

        if self.enable_logging and self.log_file:
            with open(self.log_file, "a", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(
                    [
                        timestamp,
                        elapsed_ms,
                        "STATE_3C18",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        state_code if state_code is not None else "",
                        raw_hex,
                        raw_hex,
                        "",
                    ]
                )

        self.state_count += 1
        self.raw_eda_buffer.append(
            {
                "count": self.state_count,
                "data": raw_hex,
                "state_code": state_code,
                "state_name": state_name,
            }
        )

    def _live_eda_callback(self, sender, data):
        """Callback for 42dcb71b live/event notify stream."""
        timestamp = datetime.now().isoformat()
        elapsed_ms = int(self._elapsed_seconds() * 1000)
        payload_hex = data.hex()

        self.live_eda_count += 1
        self.current_live_eda_hex = payload_hex
        self.current_live_eda_len = len(data)

        if self.enable_logging and self.log_file:
            with open(self.log_file, "a", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(
                    [
                        timestamp,
                        elapsed_ms,
                        "LIVE_EDA_42DC",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        payload_hex,
                        payload_hex,
                        json.dumps(
                            {
                                "uuid": "42dcb71b-1817-43bd-8ea3-7272780a1c9f",
                                "len": len(data),
                            }
                        ),
                    ]
                )

    def notification_callback(self, sender, data):
        """Backward-compatible stress callback API."""
        parsed = self.parse_stress_packet(data)
        if parsed:
            self.current_stress = parsed["stress_percent"]
            self.current_eda_raw = parsed["eda_raw"]

    def _stress_callback(self, sender, data):
        parsed_batch = self._parse_468f_imu_batch(data)
        if not parsed_batch:
            return

        timestamp = datetime.now().isoformat()
        elapsed_ms = int(self._elapsed_seconds() * 1000)

        waveform_hex = data[8:].hex()
        full_hex = data.hex()

        if self.enable_logging and self.log_file:
            with open(self.log_file, "a", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(
                    [
                        timestamp,
                        elapsed_ms,
                        "IMU_BATCH_468F",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        parsed_batch["clock"],
                        parsed_batch["context"],
                        parsed_batch["first_x"],
                        parsed_batch["first_y"],
                        parsed_batch["first_z"],
                        f"{parsed_batch['motion_intensity']:.2f}",
                        "",
                        waveform_hex,
                        full_hex,
                        "",
                    ]
                )

        self.imu_batch_count += 1
        self.current_eda_raw = waveform_hex
        self.imu_batch_buffer.append(
            {
                "count": self.imu_batch_count,
                "clock": parsed_batch["clock"],
                "context": parsed_batch["context"],
                "first_x": parsed_batch["first_x"],
                "first_y": parsed_batch["first_y"],
                "first_z": parsed_batch["first_z"],
                "motion_intensity": parsed_batch["motion_intensity"],
                "sample_count": len(parsed_batch["samples"]),
            }
        )

        self._update_display()

    def _update_display(self):
        if self.clear_console:
            os.system("cls" if os.name == "nt" else "clear")

        elapsed = self._elapsed_seconds()
        d306_hz = self.d306_count / elapsed
        imu_batch_hz = self.imu_batch_count / elapsed
        state_hz = self.state_count / elapsed
        live_eda_hz = self.live_eda_count / elapsed

        print("=" * 110)
        print("NUANIC MONOLITHIC MONITOR")
        print("=" * 110)
        print(
            f"Elapsed: {elapsed:.1f}s | D306: {self.d306_count} pkts ({d306_hz:.1f} Hz) | "
            f"468F IMU: {self.imu_batch_count} pkts ({imu_batch_hz:.1f} Hz) | "
            f"State (3c18): {self.state_count} pkts ({state_hz:.1f} Hz) | "
            f"LIVE_EDA (42dc): {self.live_eda_count} pkts ({live_eda_hz:.1f} Hz)"
        )
        print(
            "UUID map: "
            "STATE_UUID=3c180fcc... | LIVE_DNA_UUID=d306262b... | "
            "LIVE_EDA_UUID=42dcb71b... | STORAGE_UUID=7c3b82e7..."
        )
        print(f"D306 Context (latest): {self.current_d306_context}")
        if isinstance(self.current_dne_stress_index, int):
            print(f"DNE Stress Index (latest): {self.current_dne_stress_index}/100")
        else:
            print("DNE Stress Index (latest): unknown")
        if self.current_3c18_state_code is not None:
            print(
                f"3C18 State (latest): {self.current_3c18_state_name} "
                f"(0x{self.current_3c18_state_code:02X})"
            )
        else:
            print("3C18 State (latest): unknown")
        if self.current_live_eda_hex:
            print(
                f"42DC LIVE_EDA (latest): len={self.current_live_eda_len} hex={self.current_live_eda_hex[:64]}"
            )
        else:
            print("42DC LIVE_EDA (latest): no packets yet")
        print("=" * 110)

        print("\n[468F DATA] BATCHED IMU (14 x XYZ @ 14Hz)")
        print("-" * 110)
        if self.imu_batch_buffer:
            print(
                f"{'Pkt':<6} {'Clock':<12} {'X0':<8} {'Y0':<8} {'Z0':<8} {'Intensity':<10} {'Frames':<7}"
            )
            print("-" * 110)
            for sample in list(self.imu_batch_buffer):
                print(
                    f"#{sample['count']:<5} {sample['clock']:>11} {sample['first_x']:>7} {sample['first_y']:>7} "
                    f"{sample['first_z']:>7} {sample['motion_intensity']:>9.2f} {sample['sample_count']:>7}"
                )

        print("\n[D306 DATA] CLOCK + EDA + QUALITY")
        print("-" * 110)
        if self.d306_buffer:
            print(
                f"{'Pkt':<6} {'Clock':<12} {'EDA Value':<11} {'Context':<11} {'DNE(0-100)':<10}"
            )
            print("-" * 110)
            for sample in list(self.d306_buffer):
                print(
                    f"#{sample['count']:<5} {sample['clock']:>11} {sample['eda_value']:>10} "
                    f"{sample['context']:>10} {sample['dne_stress_index']:>10}"
                )

        print("\n" + "=" * 110)
        print("Press Ctrl+C to stop")
        print("=" * 110)

    async def start_monitoring(self):
        """Backward-compatible API for stress-only monitoring."""
        if not await self.connector.connect():
            return False

        battery = await self.connector.read_battery()
        if battery is not None:
            print(f"Battery: {battery}%")

        return await self.connector.subscribe_to_stress(self.notification_callback)

    async def stop_monitoring(self):
        """Backward-compatible API for stress-only monitoring."""
        await self.connector.unsubscribe_from_stress()
        await self.connector.disconnect()

    def get_current_stress(self):
        """Get latest stress percentage."""
        return self.current_stress

    def get_current_eda(self):
        """Get latest EDA hex payload."""
        return self.current_eda_raw

    async def run(self, duration_seconds=None):
        self.start_time = datetime.now()

        if not await self.connector.connect():
            print("[FAIL] Could not connect to ring")
            return False

        try:
            service_uuids = [service.uuid for service in self.connector.client.services]
            self._detected_profile = detect_ring_profile_from_service_uuids(
                service_uuids
            )
            print(f"[PROFILE] Detected ring profile: {self._detected_profile}")

            # Initialize log only when we are ready to start an active stream path.
            self._create_log_files()

            if self._detected_profile == MOODMETRIC_PROFILE:
                return await self._run_moodmetric_monitor(
                    duration_seconds=duration_seconds
                )

            if self._detected_profile == UNKNOWN_PROFILE:
                print("[WARN] Unknown ring profile; trying Nuanic subscriptions")

            imu_ok = await self.connector.subscribe_to_imu(self._imu_callback)
            stress_ok = await self.connector.subscribe_to_stress(self._stress_callback)
            raw_eda_ok = await self.connector.subscribe_to_raw_eda(
                self._raw_eda_callback
            )
            live_eda_ok = await self.connector.subscribe_to_live_eda(
                self._live_eda_callback
            )
            if not (imu_ok and stress_ok and raw_eda_ok and live_eda_ok):
                print("[FAIL] Could not subscribe to all streams")
                return False

            battery = await self.connector.read_battery()
            if battery is not None:
                print(f"Battery: {battery}%")

            print("[OK] Monitoring started")

            try:
                if duration_seconds is None:
                    while True:
                        await asyncio.sleep(1)
                else:
                    await asyncio.sleep(duration_seconds)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\n[STOP] Stopping capture...")
            finally:
                await self.connector.unsubscribe_from_imu()
                await self.connector.unsubscribe_from_stress()
                await self.connector.unsubscribe_from_raw_eda()
                await self.connector.unsubscribe_from_live_eda()
        finally:
            await self.connector.disconnect()

        elapsed = self._elapsed_seconds()
        print("\n" + "=" * 80)
        print("SESSION COMPLETE")
        print("=" * 80)
        print(
            f"D306 packets: {self.d306_count} ({self.d306_count / elapsed:.2f} Hz avg)"
        )
        print(
            f"468F IMU packets: {self.imu_batch_count} ({self.imu_batch_count / elapsed:.2f} Hz avg)"
        )
        print(
            f"3C18 state packets: {self.state_count} ({self.state_count / elapsed:.2f} Hz avg)"
        )
        print(
            f"42DC live EDA packets: {self.live_eda_count} ({self.live_eda_count / elapsed:.2f} Hz avg)"
        )
        print(
            f"Combined: {(self.d306_count + self.imu_batch_count + self.state_count + self.live_eda_count) / elapsed:.2f} Hz avg"
        )
        if self.enable_logging and self.log_file:
            print(f"Log CSV: {self.log_file}")
        else:
            print("Log CSV: disabled")
        print("=" * 80)
        return True

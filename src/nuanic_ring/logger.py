"""Data logging for Nuanic ring measurements"""

import csv
from pathlib import Path
from datetime import datetime
from .connector import NuanicConnector


class NuanicDataLogger:
    """Logs Nuanic ring data to CSV"""

    def __init__(self, log_dir="data/nuanic_logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.csv_file = None
        self.csv_writer = None
        self.connector = NuanicConnector()  # Ring selection happens at connection time
        self.row_count = 0

    def _create_log_file(self):
        """Create timestamped CSV file"""
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.csv_file = self.log_dir / f"nuanic_{timestamp}.csv"

        with open(self.csv_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "timestamp",
                    "stress_raw",
                    "stress_percent",
                    "eda_hex",
                    "full_packet_hex",
                ]
            )

        print(f"[LOG] Created: {self.csv_file}\n")

    def notification_callback(self, sender, data):
        """Handle incoming notifications and log"""
        if len(data) < 15:
            return

        timestamp = datetime.now().isoformat()
        stress_raw = data[14]
        stress_percent = (stress_raw / 255) * 100
        eda_hex = data[15:].hex() if len(data) > 15 else ""
        full_hex = data.hex()

        # Write to CSV
        with open(self.csv_file, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [timestamp, stress_raw, f"{stress_percent:.1f}", eda_hex, full_hex]
            )

        self.row_count += 1

        # Print progress every 10 readings
        if self.row_count % 10 == 0:
            print(
                f"[LOG] Logged {self.row_count} readings... Latest stress: {stress_percent:.1f}%"
            )

    async def check_ring_mac_address(self, num_scans: int = 5):
        """Check if ring(s) have dynamic or static MAC addresses.

        Useful for diagnosing connection issues.
        """
        result = await self.connector.check_mac_address_dynamic(num_scans=num_scans)
        return result

    async def start_logging(self, duration_seconds=None):
        """Start logging Nuanic data"""
        self._create_log_file()

        if not await self.connector.connect():
            return False

        # Subscribe IMMEDIATELY after connection (no delays!)
        if not await self.connector.subscribe_to_stress(self.notification_callback):
            return False

        print("=" * 80)
        print("LOGGING NUANIC RING DATA")
        print("=" * 80)
        if duration_seconds:
            print(f"Duration: {duration_seconds} seconds")
        else:
            print("Duration: unlimited (Ctrl+C to stop)")
        print("=" * 80 + "\n")

        # Try battery read (non-blocking)
        battery = await self.connector.read_battery()
        if battery:
            print(f"Battery: {battery}%\n")

        try:
            if duration_seconds:
                import asyncio

                await asyncio.sleep(duration_seconds)
            else:
                # Wait indefinitely
                await self.connector.client.start_notify(
                    self.connector.STRESS_CHARACTERISTIC, self.notification_callback
                )
                import asyncio

                await asyncio.sleep(float("inf"))
        except KeyboardInterrupt:
            print("\n[STOP] User interrupted")
        except Exception as e:
            print(f"\n[ERROR] {e}")

        await self.stop_logging()
        return True

    async def stop_logging(self):
        """Stop logging and disconnect"""
        await self.connector.unsubscribe_from_stress()
        await self.connector.disconnect()

        print(f"\n[LOG] Session complete!")
        print(f"[LOG] Total readings: {self.row_count}")
        print(f"[LOG] File: {self.csv_file}\n")

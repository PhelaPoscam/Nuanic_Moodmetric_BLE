"""Ring profile definitions and detection helpers."""

from typing import Iterable, List

NUANIC_PROFILE = "nuanic"
MOODMETRIC_PROFILE = "moodmetric"
UNKNOWN_PROFILE = "unknown"

NUANIC_SERVICE_UUID = "5491faaf-b0c2-4167-8f3d-bc6b31db69e7"
MOODMETRIC_SERVICE_UUIDS = {
    "dd499b70-e4cd-4988-a923-a7aab7283f8e",
    "aed4978e-9c7a-11e3-8d05-425861b86ab6",
}

# Standard Nuanic Characteristic UUIDs
STATE_UUID = "3c180fcc-bfec-4b7c-8e52-1a37f123e449"
SAMPLE_RATE_UUID = "516b0fb6-d861-4619-9dd0-0105e8b85128"
REALTIME_UUID = "dc9c31a7-fbd3-467a-8777-10900c423d3b"
LIVE_DNE_UUID = "d306262b-c8c9-4c4b-9050-3a41dea706e5"
LIVE_EDA_UUID = "42dcb71b-1817-43bd-8ea3-7272780a1c9f"
STORAGE_UUID = "7c3b82e7-22b7-4cb6-8458-ba325edf6ede"
STORAGE_FORMAT_UUID = "3cce21a7-e602-4e02-8c52-1e0366c1c846"
STORAGE_REWIND_UUID = "2175c13f-60e4-4de5-80af-0d06f1b54880"
STORAGE_USAGE_UUID = "d78e5bd8-53d6-4fc3-bc98-03b8cd71684b"
COMMAND_UUID = "741f0d15-cc3d-4715-a9fb-a5a6bccebc50"
IMU_BATCH_UUID = "468f2717-6a7d-46f9-9eb7-f92aab208bae"
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

NOTIFY_UUIDS_BY_PROFILE = {
    NUANIC_PROFILE: [
        "42dcb71b-1817-43bd-8ea3-7272780a1c9f",
        "d306262b-c8c9-4c4b-9050-3a41dea706e5",
        "3c180fcc-bfec-4b7c-8e52-1a37f123e449",
        "468f2717-6a7d-46f9-9eb7-f92aab208bae",
    ],
    MOODMETRIC_PROFILE: [
        "a0956420-9bd2-11e4-bd06-0800200c9a66",
        "c48650d0-a2d8-11e4-bcd8-0800200c9a66",
        "90bd4fd0-4309-11e4-916c-0800200c9a66",
        "f1b41cde-dbf5-4acf-8679-ecb8b4dca6ff",
        "5d7a90a0-ab7e-11e4-bcd8-0800200c9a66",
    ],
}


def detect_ring_profile_from_service_uuids(service_uuids: Iterable[str]) -> str:
    """Detect ring profile from discovered service UUIDs."""
    uuids = {u.lower() for u in service_uuids}

    if NUANIC_SERVICE_UUID in uuids:
        return NUANIC_PROFILE

    if any(u in uuids for u in MOODMETRIC_SERVICE_UUIDS):
        return MOODMETRIC_PROFILE

    return UNKNOWN_PROFILE


def notify_uuids_for_profile(profile: str) -> List[str]:
    """Return known notify UUIDs for the given profile."""
    return list(NOTIFY_UUIDS_BY_PROFILE.get(profile, []))

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

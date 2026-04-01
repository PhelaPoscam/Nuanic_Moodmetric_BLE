from nuanic_ring.ring_profiles import (
    detect_ring_profile_from_service_uuids,
    NUANIC_PROFILE,
    MOODMETRIC_PROFILE,
    UNKNOWN_PROFILE,
)

def test_detect_nuanic_profile():
    uuids = [
        "c8c0a708-e361-4b5e-a365-98fa66c9ff66", 
        "5491faaf-b0c2-4167-8f3d-bc6b31db69e7" 
    ]
    assert detect_ring_profile_from_service_uuids(uuids) == NUANIC_PROFILE

def test_detect_moodmetric_profile():
    uuids = [
        "dd499b70-e4cd-4988-a923-a7aab7283f8e"
    ]
    assert detect_ring_profile_from_service_uuids(uuids) == MOODMETRIC_PROFILE

def test_detect_unknown_profile():
    uuids = ["11111111-2222-3333-4444-555555555555"]
    assert detect_ring_profile_from_service_uuids(uuids) == UNKNOWN_PROFILE

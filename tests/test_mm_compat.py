import datetime
from nuanic_ring.mm_compat import MMLikeScorer, MMFeatures, decode_raw_resistance_packet

def test_mmlike_scorer_calibration():
    # 2 seconds calibration for testing
    scorer = MMLikeScorer(calibration_seconds=2)
    
    f = MMFeatures(scr_frequency_per_min=5.0, scr_amplitude=0.5, scl_microsiemens=8.0)
    
    start_time = datetime.datetime.now()
    
    # Initial processing should register, but not mark as fully calibrated
    state = scorer.update(f, now=start_time)
    assert state["calibrated"] is False
    assert state["mm_like_1_to_100"] == 0.0
    
    f2 = MMFeatures(scr_frequency_per_min=0.0, scr_amplitude=0.0, scl_microsiemens=0.0)
    
    # Advance time past calibration_seconds (Scorer enforces 10s minimum clamp internally)
    later = start_time + datetime.timedelta(seconds=11)
    state = scorer.update(f2, now=later)
    
    assert state["calibrated"] is True
    # Verify bounds of the scale
    assert 1.0 <= state["mm_like_1_to_100"] <= 100.0

def test_decode_raw_resistance():
    # Pack raw_value = 100 -> equivalent to 0x00, 0x64
    packet = bytes([0x00, 0x64])
    res = decode_raw_resistance_packet(packet)
    assert res is not None
    assert res["raw_skin_resistance_value"] == 100
    assert res["skin_resistance_ohms"] > 0
    assert res["skin_conductance_siemens"] > 0

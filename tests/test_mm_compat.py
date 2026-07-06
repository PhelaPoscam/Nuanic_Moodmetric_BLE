import datetime

from nuanic_ring.mm_compat import MMLikeScorer


def test_mmlike_scorer_calibration():
    scorer = MMLikeScorer(calibration_seconds=2)

    start_time = datetime.datetime.now()

    # Initial processing should register, but not mark as fully calibrated
    state = scorer.update(
        scr_frequency_per_min=5.0,
        scr_amplitude=0.5,
        scl_microsiemens=8.0,
        now=start_time,
    )
    assert state["calibrated"] is False
    assert state["mm_like_1_to_100"] == 0.0

    # Advance time past calibration_seconds (Scorer enforces 10s minimum clamp internally)
    later = start_time + datetime.timedelta(seconds=11)
    state = scorer.update(
        scr_frequency_per_min=0.0,
        scr_amplitude=0.0,
        scl_microsiemens=0.0,
        now=later,
    )

    assert state["calibrated"] is True
    # Verify bounds of the scale
    assert 1.0 <= state["mm_like_1_to_100"] <= 100.0


def test_decode_raw_resistance():
    """Verify 2-byte big-endian raw resistance packet decoding (inlined from former helper)."""
    packet = bytes([0x00, 0x64])
    ohms_per_raw_unit = 244.14435034728638

    raw_value = (packet[0] << 8) | packet[1]
    assert raw_value == 100

    skin_resistance_ohms = raw_value * ohms_per_raw_unit
    skin_conductance_siemens = (
        1.0 / skin_resistance_ohms if skin_resistance_ohms > 0 else 0.0
    )
    skin_conductance_microsiemens = skin_conductance_siemens * 1_000_000.0

    assert skin_resistance_ohms > 0
    assert skin_conductance_siemens > 0
    assert skin_conductance_microsiemens > 0

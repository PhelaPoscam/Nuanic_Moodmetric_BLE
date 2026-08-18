"""Tests for modular package imports and backward-compatibility shims."""

import pytest


def test_core_submodule_imports():
    from nuanic_ring.core import NuanicConnector, RingScanner
    from nuanic_ring.core.connector import NuanicConnector as CoreConnector
    from nuanic_ring.core.profiles import MOODMETRIC_PROFILE, NUANIC_PROFILE
    from nuanic_ring.core.scanner import RingScanner as CoreScanner

    assert NuanicConnector is CoreConnector
    assert RingScanner is CoreScanner
    assert NUANIC_PROFILE == "nuanic"
    assert MOODMETRIC_PROFILE == "moodmetric"


def test_dsp_submodule_imports():
    from nuanic_ring.dsp import SignalConditioner
    from nuanic_ring.dsp.signal_processing import SignalConditioner as DspConditioner

    assert SignalConditioner is DspConditioner
    sc = SignalConditioner(sample_rate=16.0)
    assert sc.process(10.0) is not None


def test_io_submodule_imports():
    from nuanic_ring.io import (
        CombinedLogRow,
        ComputedLogRow,
        D306Packet,
        FingerStatePacket,
        ImuBatchPacket,
        ImuLogRow,
        LiveEdaPacket,
        NuanicExportLogRow,
        SessionManifest,
        StreamLogRow,
        generate_session_manifest,
    )
    from nuanic_ring.io.schemas import CombinedLogRow as IoCombined

    assert CombinedLogRow is IoCombined


def test_telemetry_submodule_imports():
    from nuanic_ring.telemetry import (
        RingDeviceState,
        build_row_rate_tail,
        equalize_decision,
        get_smoothed_time,
        nuanic_ts_fields,
        parse_468f_imu_batch,
        parse_d306_packet,
        parse_finger_state_packet,
        parse_live_eda_packet,
        update_observed_hz,
    )

    state = RingDeviceState(mac="AA:BB:CC:DD:EE:01")
    assert state.mac == "AA:BB:CC:DD:EE:01"


def test_root_backward_compatibility_shims():
    import nuanic_ring._scanner as legacy_scan
    import nuanic_ring.connector as legacy_conn
    import nuanic_ring.manifest as legacy_man
    import nuanic_ring.ring_profiles as legacy_prof
    import nuanic_ring.schemas as legacy_sch
    import nuanic_ring.signal_processing as legacy_dsp
    from nuanic_ring.core.connector import NuanicConnector
    from nuanic_ring.core.scanner import RingScanner
    from nuanic_ring.dsp.signal_processing import SignalConditioner

    assert legacy_conn.NuanicConnector is NuanicConnector
    assert legacy_scan.RingScanner is RingScanner
    assert legacy_dsp.SignalConditioner is SignalConditioner

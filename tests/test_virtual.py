"""Virtual backend tests. No hardware — run with: pytest from Backprobe/.

Covers the four things the virtual backend exists to test (PHASE_1_VIRTUAL_DESIGN):
the full SCANNING→ATTACHED path against a preset, the multi-ECU census, the
probe loop advancing past a wrong profile, and a decode↔encode round-trip.

The encoders are anchored to REAL, non-PII OldVer wire bytes (support masks,
MIL, Mode 06) hardcoded below — the logs themselves are git-ignored, so the
bytes live here as permanent, documented anchors. If decode.py and these
encoders ever share a wrong assumption, these literal-byte asserts catch it.
"""

import pytest

from backprobe.obd2 import decode
from backprobe.transport import virtual as v
from backprobe.transport.base import (
    ConnectError,
    ConnectProfile,
    DeviceBusy,
    DeviceLost,
    Timeout,
)

P11_500 = ConnectProfile("ISO15765", 500000, "11-bit")
P29_500 = ConnectProfile("ISO15765", 500000, "29-bit")
P11_250 = ConnectProfile("ISO15765", 250000, "11-bit")

# Real OldVer wire bytes (non-PII), captured from the seeding sessions.
REAL_A_0100 = bytes.fromhex("4100be3f8003")   # sedan_mil_on, support group 0
REAL_A_0120 = bytes.fromhex("412000020001")   # support group 0x20
REAL_A_0140 = bytes.fromhex("414000008010")   # support group 0x40
REAL_A_0101 = bytes.fromhex("41018207e100")   # MIL on, 2 DTCs
REAL_B_0601 = bytes.fromhex(                   # truck_healthy, 5 records, MID 1
    "4601918d0196006e022e01938a064501c47fff01948afb018000fe15"
    "019590001000000167019690000000000167"
)


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path):
    """Send the backend's session log to a throwaway dir so tests leave no
    stray ./logs. Patches the exact module object virtual.py logs through."""
    v.session_log._log_file = None
    v.session_log.init(tmp_path / "logs")


@pytest.fixture()
def session():
    """An opened virtual session, nothing attached yet."""
    return v.VirtualBackend().open(v._DEVICE)


# ─── encoder anchors: real bytes + round-trips ─────────────────────────────


def test_support_mask_encoder_matches_real_bytes():
    a = v.PRESETS["sedan_mil_on"].main_ecu
    assert v._encode_support_mask(0x41, 0x00, a.supported_pids) == REAL_A_0100
    assert v._encode_support_mask(0x41, 0x20, a.supported_pids) == REAL_A_0120
    assert v._encode_support_mask(0x41, 0x40, a.supported_pids) == REAL_A_0140


def test_mil_encoder_matches_real_bytes():
    a = v.PRESETS["sedan_mil_on"].main_ecu
    assert v._encode_pid01(a.mil_on, a.dtc_count, a.readiness) == REAL_A_0101


def test_mode06_encoder_matches_real_bytes():
    b = v.PRESETS["truck_healthy"].main_ecu
    assert v._encode_mode06(b.mode06) == REAL_B_0601


def test_vin_round_trips_through_decode():
    a = v.PRESETS["sedan_mil_on"].main_ecu
    assert a.vin is not None                           # narrows str | None for _encode_vin
    assert decode.decode_vin(v._encode_vin(a.vin)) == a.vin


def test_supported_pid_walk_round_trips():
    """Encode each group, decode it, follow more-groups — the reassembled set
    must equal the vehicle's declared PIDs (encoder + bitmask walk + decoder)."""
    ecu = v.PRESETS["truck_healthy"].main_ecu
    found, base = [], 0x00
    while True:
        _, sup, more = decode.decode_supported_pids(
            v._encode_support_mask(0x41, base, ecu.supported_pids)
        )
        found += sup
        if not more:
            break
        base += 0x20
    assert set(found) == ecu.supported_pids


# ─── device / session lifecycle ────────────────────────────────────────────


def test_enumerate_returns_one_device():
    assert v.VirtualBackend().enumerate() == [v._DEVICE]


def test_open_when_busy_raises_device_busy():
    with pytest.raises(DeviceBusy):
        v.VirtualBackend(busy=True).open(v._DEVICE)


def test_voltage_tracks_plug_in_and_unplug(session):
    assert session.read_voltage() < 9_000             # no vehicle → low
    session.plug_in(v.get_preset("sedan_mil_on"))
    assert session.read_voltage() > 9_000             # attached → high
    session.unplug()
    assert session.read_voltage() < 9_000             # back to low


def test_close_is_idempotent_and_voltage_then_raises(session):
    session.close()
    session.close()                                   # no error second time
    with pytest.raises(DeviceLost):
        session.read_voltage()


# ─── probe loop (Decision 3) ───────────────────────────────────────────────


def test_connect_without_vehicle_raises(session):
    with pytest.raises(ConnectError):
        session.connect(P11_500)


def test_probe_loop_rejects_wrong_profiles_then_connects(session):
    """A 29-bit vehicle must reject the 11-bit profiles the daemon tries first,
    so the probe loop is genuinely exercised, then accept 29-bit."""
    session.plug_in(v.get_preset("diesel_29bit"))
    for wrong in (P11_500, P11_250):
        with pytest.raises(ConnectError):
            session.connect(wrong)
    channel = session.connect(P29_500)                # the matching profile wins
    assert channel is not None


# ─── the harvest path + census ─────────────────────────────────────────────


def test_full_harvest_against_preset(session):
    session.plug_in(v.get_preset("sedan_mil_on"))
    ch = session.connect(P11_500)
    assert decode.decode_vin(ch.ask(b"\x09\x02", 1.0).payload) == "1HGCM82633A004321"
    assert decode.decode_mil_status(ch.ask(b"\x01\x01", 1.0).payload) == (True, 2)
    _, sup, _ = decode.decode_supported_pids(ch.ask(b"\x01\x00", 1.0).payload)
    assert 0x0C in sup and 0x05 in sup
    rpm = decode.decode_pid_value(ch.ask(b"\x01\x0c", 1.0).payload)
    assert rpm.name == "engine_rpm" and rpm.value == 800.0
    tests = decode.decode_mode06(ch.ask(b"\x06\x01", 1.0).payload)
    assert tests and tests[0].mid == 1


def test_multi_ecu_census(session):
    session.plug_in(v.get_preset("truck_healthy"))
    ch = session.connect(P11_500)
    replies = ch.ask_all(b"\x01\x00", 1.0)
    assert sorted(r.ecu for r in replies) == [0x7E8, 0x7E9]


def test_ask_unsupported_request_times_out(session):
    session.plug_in(v.get_preset("sedan_mil_on"))
    ch = session.connect(P11_500)
    with pytest.raises(Timeout):
        ch.ask(b"\x01\xfe", 1.0)                       # PID 0xFE: nobody answers


def test_ask_all_unsupported_is_empty_not_error(session):
    session.plug_in(v.get_preset("sedan_mil_on"))
    ch = session.connect(P11_500)
    assert ch.ask_all(b"\x01\xfe", 1.0) == []          # [] is a valid census answer


def test_disconnect_is_idempotent_and_ask_then_raises(session):
    session.plug_in(v.get_preset("sedan_mil_on"))
    ch = session.connect(P11_500)
    ch.disconnect()
    ch.disconnect()                                    # no error second time
    with pytest.raises(DeviceLost):
        ch.ask(b"\x09\x02", 1.0)

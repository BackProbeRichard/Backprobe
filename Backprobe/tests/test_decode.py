"""Pure-decode regression tests. No hardware — run with: pytest from Backprobe/.

Synthetic OBD responses in, decoded meaning out. Anchored where possible to
the values in PHASE_1_DOCS/PHASE_1_LOG_MOCKUP.jsonl so the decoders and the
agreed output contract can't drift apart.
"""

import pytest

from backprobe.obd2 import decode, pids


def _m01(pid: int, *data: int) -> bytes:
    """A Mode 01 positive response: 0x41, pid, then data bytes."""
    return bytes([0x41, pid, *data])


# ─── supported-PID bitmask ─────────────────────────────────────────────────


def test_supported_pids_matches_mockup_bitmap():
    # 0xBE1FB413 is the engine ECU's pid_bitmap_0x00 from the log mockup.
    base, supported, more = decode.decode_supported_pids(bytes.fromhex("4100BE1FB413"))
    assert base == 0x00
    assert more is True
    # The two the old 17-entry table couldn't name, now reported honestly:
    assert 0x01 in supported
    assert 0x03 in supported
    assert {0x04, 0x05, 0x06, 0x07, 0x0C, 0x0D, 0x0E, 0x0F}.issubset(supported)


def test_supported_pids_more_groups_false_when_lsb_clear():
    _, _, more = decode.decode_supported_pids(bytes.fromhex("412000000000"))
    assert more is False


def test_supported_pids_rejects_wrong_service():
    with pytest.raises(decode.DecodeError):
        decode.decode_supported_pids(bytes.fromhex("4900BE1FB413"))


# ─── MIL + DTC count ───────────────────────────────────────────────────────


def test_mil_on_with_two_dtcs():
    mil, count = decode.decode_mil_status(_m01(0x01, 0x82, 0x00, 0x00, 0x00))
    assert mil is True
    assert count == 2


def test_mil_off_no_dtcs():
    mil, count = decode.decode_mil_status(_m01(0x01, 0x00, 0x07, 0xE5, 0x00))
    assert mil is False
    assert count == 0


# ─── VIN ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("vin", ["5FNRL6H54LB123456", "1GTGG6B30F1268637"])
def test_vin_roundtrip_from_mockup(vin):
    resp = bytes([0x49, 0x02, 0x01]) + vin.encode("ascii")
    assert decode.decode_vin(resp) == vin


def test_vin_strips_padding():
    resp = bytes([0x49, 0x02, 0x01]) + b"ABC123\x00\x00"
    assert decode.decode_vin(resp) == "ABC123"


# ─── live PID value decode ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "resp, value, unit",
    [
        (_m01(0x05, 0x5A), 50, "°C"),            # coolant: 90 - 40
        (_m01(0x0C, 0x1A, 0xF8), 1726.0, "rpm"),  # rpm: 0x1AF8 / 4
        (_m01(0x0D, 0x50), 80, "km/h"),
        (_m01(0x11, 0xFF), 100.0, "%"),          # throttle wide open
        (_m01(0x46, 0x37), 15, "°C"),            # ambient: 55 - 40
        (_m01(0x42, 0x32, 0x1B), 12.827, "V"),   # control module voltage
        (_m01(0x51, 0x04), "Diesel", ""),        # fuel type enumeration
    ],
)
def test_pid_value_scalars(resp, value, unit):
    r = decode.decode_pid_value(resp)
    if isinstance(value, (int, float)):
        assert abs(r.value - value) < 0.001
    else:
        assert r.value == value
    assert r.unit == unit


def test_pid_value_multifield_is_named_but_valueless():
    r = decode.decode_pid_value(_m01(0x24, 0x80, 0x00, 0x40, 0x00))
    assert r.name == "o2_sensor_1_lambda"
    assert r.value is None          # multi-field: named, not faked


def test_pid_value_unknown_pid_keeps_raw():
    r = decode.decode_pid_value(_m01(0xF0, 0xAB))
    assert r.name is None
    assert r.raw == bytes([0xAB])   # nothing hidden


# ─── Mode 06 ───────────────────────────────────────────────────────────────


def test_mode06_o2_min_voltage_matches_mockup():
    # MID 0x01 / TID 0x01, scaling 0x0B (1 mV/bit): 0x034D = 845 -> 0.845 V.
    resp = bytes([0x46, 0x01, 0x01, 0x0B, 0x03, 0x4D, 0x00, 0x00, 0x03, 0xE8])
    (t,) = decode.decode_mode06(resp)
    assert t.mid == 0x01 and t.tid == 0x01
    assert abs(t.value - 0.845) < 0.001
    assert t.unit == "V"
    assert t.passed is True


def test_mode06_fail_when_value_above_max():
    # value 0x0FA0 above max 0x03E8 -> fail.
    resp = bytes([0x46, 0x01, 0x02, 0x0B, 0x0F, 0xA0, 0x00, 0x00, 0x03, 0xE8])
    (t,) = decode.decode_mode06(resp)
    assert t.passed is False


# ─── table integrity ───────────────────────────────────────────────────────


def test_every_formula_runs_clean():
    """No lambda in the table raises on a full 4-byte payload."""
    probe = bytes([0x11, 0x22, 0x33, 0x44])
    for num, p in pids.PIDS.items():
        if p.decode is None:
            continue
        p.decode(probe[: max(p.n_bytes, 1)])  # raises on a bad formula


def test_table_has_expected_shape():
    decodable = [p for p in pids.PIDS.values() if p.decode]
    assert len(pids.PIDS) > 100          # full standard set, not the old 17
    assert len(decodable) >= 60          # the everyday scalars are covered

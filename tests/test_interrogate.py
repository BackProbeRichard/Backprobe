"""Probe + harvest tests, against the virtual backend. No hardware.

Covers Decision 4 (probe wins only on a confirming reply; advances past wrong
profiles; full list twice) and Decision 5 (best-effort harvest; ≥1 ECU = a
connection; multi-ECU census). DeviceLost must propagate, not be swallowed.
"""

import json

import pytest

from backprobe import session_log
from backprobe.daemon import events as E
from backprobe.daemon import interrogate as I
from backprobe.transport import virtual as v
from backprobe.transport.base import DeviceLost


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path):
    session_log._log_file = None
    session_log.init(tmp_path / "diag")


@pytest.fixture()
def evlog(tmp_path):
    return E.EventLog(tmp_path / "events", console=False)


def _events(evlog) -> list[dict]:
    return [json.loads(ln) for ln in evlog.path.read_text().splitlines()]


def _open(preset: str | None):
    veh = v.get_preset(preset) if preset else None
    return v.VirtualBackend(attached=veh).open(v._DEVICE)


# ─── probe (Decision 4) ─────────────────────────────────────────────────────


def test_probe_wins_step_1_on_11bit_vehicle(evlog):
    pr = I.probe(_open("sedan_mil_on"), evlog)
    assert pr and pr.step == 1 and pr.profile.addressing == "11-bit"


def test_probe_advances_to_29bit(evlog):
    pr = I.probe(_open("diesel_29bit"), evlog)
    assert pr and pr.step == 2 and pr.profile.addressing == "29-bit"
    win = [e for e in _events(evlog) if e["event"] == "probe_success"][0]
    assert win["winning_step"] == 2


def test_probe_fails_after_two_full_passes(evlog):
    pr = I.probe(_open(None), evlog)                 # no vehicle → every connect rejected
    assert pr is None
    failed = [e for e in _events(evlog) if e["event"] == "probe_failed"][0]
    assert failed["attempts"] == len(I.PROBE_PROFILES) * I.PROBE_PASSES


def test_protocol_label():
    assert I.protocol_label(I.PROBE_PROFILES[0]) == "ISO15765 / CAN 11-bit 500kbps"


# ─── harvest (Decision 5) ───────────────────────────────────────────────────


def test_harvest_summary_mil_vehicle(evlog):
    s = _open("sedan_mil_on")
    pr = I.probe(s, evlog)
    summ = I.harvest(s, pr.channel, evlog)
    assert summ.vin == "1HGCM82633A004321"
    assert summ.ecu_count == 1 and summ.mil_on is True and summ.dtc_count == 2
    assert summ.mode_06_supported is True


def test_harvest_emits_full_event_sequence(evlog):
    s = _open("sedan_mil_on")
    pr = I.probe(s, evlog)
    I.harvest(s, pr.channel, evlog)
    kinds = [e["event"] for e in _events(evlog)]
    for expected in ("vin_read", "ecu_census", "supported_pids",
                     "mil_and_dtc", "mode_06_results", "voltage_read"):
        assert expected in kinds


def test_supported_pids_are_named(evlog):
    s = _open("sedan_mil_on")
    pr = I.probe(s, evlog)
    I.harvest(s, pr.channel, evlog)
    sp = [e for e in _events(evlog) if e["event"] == "supported_pids"][0]
    rpm = [p for p in sp["pids"] if p["hex"] == "0x0C"][0]
    assert rpm["name"] == "engine_rpm" and rpm["unit"] == "rpm"


def test_multi_ecu_census(evlog):
    s = _open("truck_healthy")
    pr = I.probe(s, evlog)
    summ = I.harvest(s, pr.channel, evlog)
    assert summ.ecu_count == 2
    census = [e for e in _events(evlog) if e["event"] == "ecu_census"][0]
    assert sorted(x["address"] for x in census["ecus"]) == ["0x7E8", "0x7E9"]


def test_harvest_without_ecu_returns_none(evlog, monkeypatch):
    """If the census finds no ECU, it's a failed probe, not a connection."""
    s = _open("sedan_mil_on")
    pr = I.probe(s, evlog)
    monkeypatch.setattr(pr.channel, "ask_all", lambda *a, **k: [])   # nobody answers census
    assert I.harvest(s, pr.channel, evlog) is None


def test_device_lost_propagates_through_harvest(evlog):
    s = _open("sedan_mil_on")
    pr = I.probe(s, evlog)
    pr.channel.disconnect()                          # channel dead → ask raises DeviceLost
    with pytest.raises(DeviceLost):
        I.harvest(s, pr.channel, evlog)

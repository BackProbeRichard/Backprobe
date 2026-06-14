"""Event-log writer tests. No hardware.

Guards the JSONL contract (Decision 7): valid JSON per line, mockup field names,
flush-per-event durability, and the console mirror for headline events.
"""

import json

import pytest

from backprobe.daemon import events


@pytest.fixture()
def log(tmp_path):
    return events.EventLog(tmp_path / "logs", console=False)


def _lines(log) -> list[dict]:
    return [json.loads(ln) for ln in log.path.read_text().splitlines()]


def test_every_line_is_valid_json_with_timestamp_and_event(log):
    log.emit("daemon_start", python="3.11", platform="Linux", devices=[])
    log.emit("vin_read", vin="1HGCM82633A004321")
    rows = _lines(log)
    assert len(rows) == 2
    for r in rows:
        assert "timestamp" in r and r["timestamp"].endswith("Z")
        assert "event" in r


def test_timestamp_is_iso_utc_millis(log):
    log.emit("x")
    ts = _lines(log)[0]["timestamp"]
    # 2026-06-14T12:00:00.123Z
    assert ts[10] == "T" and ts[23] == "Z" and ts[19] == "."


def test_typed_wrappers_match_mockup_fields(log):
    log.probe_success(protocol="ISO15765", bitrate=500000, addressing="11-bit",
                      winning_step=1, elapsed_ms=872.4)
    log.supported_pids(ecu="0x7E8", pids=[{"hex": "0x0C", "name": "engine_rpm", "unit": "rpm"}])
    log.connection_complete(vin="1HGCM82633A004321", protocol="ISO15765 / CAN 11-bit 500kbps",
                            ecu_count=1, mil_on=True, stored_dtc_count=2,
                            mode_06_supported=True, connected_duration_ms=3140.0)
    ps, sp, cc = _lines(log)
    assert ps["event"] == "probe_success" and ps["winning_step"] == 1
    assert sp["event"] == "supported_pids" and sp["pid_count"] == 1   # derived
    assert cc["mil_on"] is True and cc["stored_dtc_count"] == 2


def test_durability_flush_per_event_readable_before_close(tmp_path):
    log = events.EventLog(tmp_path / "logs", console=False)
    log.voltage_detected(voltage_mv=12847)
    # not closed yet — a kill here must still leave the line on disk
    assert _lines(log)[0]["voltage_mv"] == 12847


def test_vin_is_tokenized_in_event_log(log):
    """No raw serial on disk: 1-11 kept, 12-17 → stable keyed token, decodable."""
    from backprobe import session_log
    full = "1HGCM82633A004321"
    log.vin_read(vin=full)
    log.connection_complete(vin=full, protocol="x", ecu_count=1, mil_on=False,
                            stored_dtc_count=0, mode_06_supported=False,
                            connected_duration_ms=1.0)
    token = session_log.vin_token(full)
    expected = full[:11] + token
    rows = _lines(log)
    assert rows[0]["vin"] == expected                  # vin_read tokenized
    assert rows[1]["vin"] == expected                  # connection_complete, same token
    assert full not in rows[0]["vin"]                  # full VIN never on disk
    assert full[11:] not in rows[0]["vin"]             # serial never on disk
    assert session_log.decode_token(token) == full     # recoverable with the key


def test_event_count_and_daemon_stop_total(log):
    log.voltage_detected(voltage_mv=12847)
    log.voltage_dropped(voltage_mv=234)
    log.daemon_stop(uptime_seconds=2148.0, vehicles_logged=1, exit_code=0)
    assert log.event_count == 3
    assert _lines(log)[-1]["total_events"] == 3


def test_battery_health_classifier():
    assert events.battery_health(12847) == "good"
    assert events.battery_health(12100) == "ok"
    assert events.battery_health(11500) == "low"
    assert events.battery_health(234) == "weak"


def test_console_mirrors_only_headline_events(tmp_path, capsys):
    log = events.EventLog(tmp_path / "logs", console=True)
    log.voltage_detected(voltage_mv=12847)        # headline -> printed
    log.vin_read(vin="1HGCM82633A004321")          # not headline -> silent
    out = capsys.readouterr().out
    assert "voltage_detected" in out
    assert "vin_read" not in out


def test_close_is_idempotent(log):
    log.close()
    log.close()

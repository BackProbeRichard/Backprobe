"""The JSON-lines connection-event log (PHASE_1_DAEMON_DESIGN Decision 7).

This is the daemon's product output — the structured event stream a consumer
reads — distinct from session_log.py (the deep diagnostic text log). One file
per daemon run, one JSON object per line, flushed after every write so a kill
on the bench loses nothing.

The event names and fields follow PHASE_1_LOG_MOCKUP.jsonl (the contract). The
typed methods below ARE that schema — callers can't typo a field name. Two
deliberate departures from the mockup, which used illustrative non-valid JSON:
hex-conventional IDs (MID/TID/PID/ECU) are emitted as hex STRINGS ("0x7E8") so
the output is valid JSON, matching the mockup's own pid {"hex": ...} / ECU
"address" style.

VIN policy: VINs are TOKENIZED before they hit disk, the same scheme as
session_log — positions 1-11 (make/model/year/plant) stay readable, the serial
(12-17) is replaced by a stable keyed token, recorded in the local keyring so
the same vehicle is recognizable across logs and the full VIN is recoverable
with the key (python -m backprobe.session_log --decode <token>). No raw serial
reaches disk. The VIN passes through exactly one helper, _partial_vin().
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from backprobe import session_log


def _partial_vin(vin: str | None) -> str | None:
    """Make/model/year/plant kept (1-11), serial (12-17) → stable keyed token,
    recorded in the keyring. Same scheme and key as session_log, so the token
    matches across both logs."""
    if not vin:
        return vin
    return session_log.tokenize_vins(vin)

# Headline events mirrored to the console for live watching (Decision 7).
_HEADLINE = {
    "daemon_start", "device_opened", "voltage_detected", "probe_success",
    "connection_complete", "voltage_dropped", "device_released", "daemon_stop",
}


def _now() -> str:
    """ISO-8601 UTC with millisecond precision and a trailing Z, as in the mockup."""
    dt = datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def battery_health(voltage_mv: int) -> str:
    """Coarse 12 V-system classification for the voltage_read event."""
    if voltage_mv >= 12_400:
        return "good"
    if voltage_mv >= 12_000:
        return "ok"
    if voltage_mv >= 11_400:
        return "low"
    return "weak"


class EventLog:
    """Append-only JSONL writer, one file per daemon run. Flush per event."""

    def __init__(self, log_dir: str | Path, *, console: bool = True) -> None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self._path = directory / f"events_{stamp}.jsonl"
        self._fh = self._path.open("a", encoding="utf-8")
        self._console = console
        self.event_count = 0

    @property
    def path(self) -> Path:
        return self._path

    # ─── core ──────────────────────────────────────────────────────────────

    def emit(self, event: str, **fields: Any) -> dict:
        """Write one event line (timestamp + event + fields), flush, mirror if
        headline. Never raises into the caller — the daemon must outlive a log
        hiccup (fail loudly, but to stderr, not by crashing)."""
        record = {"timestamp": _now(), "event": event, **fields}
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception as exc:  # noqa: BLE001 - last-resort, never kill the daemon
            import sys
            print(f"event-log write failed ({event}): {exc}", file=sys.stderr)
        self.event_count += 1
        if self._console and event in _HEADLINE:
            print(_console_line(record))
        return record

    def close(self) -> None:
        """Idempotent, never raises."""
        try:
            self._fh.close()
        except Exception:
            pass

    # ─── typed wrappers = the event schema (names/fields per the mockup) ─────

    def daemon_start(self, *, python: str, platform: str, devices: list[dict]) -> None:
        self.emit("daemon_start", python=python, platform=platform,
                  j2534_devices_found=len(devices), devices=devices)

    def device_opened(self, *, device: str, device_id: int,
                      firmware: str | None, dll_version: str | None) -> None:
        self.emit("device_opened", device=device, device_id=device_id,
                  firmware=firmware, dll_version=dll_version)

    def voltage_detected(self, *, voltage_mv: int) -> None:
        self.emit("voltage_detected", voltage_mv=voltage_mv,
                  status="vehicle_plugged_in")

    def probe_start(self, *, attempts: list[dict]) -> None:
        self.emit("probe_start", attempts=attempts)

    def probe_success(self, *, protocol: str, bitrate: int, addressing: str,
                      winning_step: int, elapsed_ms: float) -> None:
        self.emit("probe_success", protocol=protocol, bitrate=bitrate,
                  addressing=addressing, winning_step=winning_step,
                  elapsed_ms=round(elapsed_ms, 1))

    def probe_failed(self, *, attempts: int) -> None:
        """No profile won after the full probe (Decision 4: two full passes)."""
        self.emit("probe_failed", attempts=attempts, status="no_protocol")

    def vin_read(self, *, vin: str) -> None:
        self.emit("vin_read", vin=_partial_vin(vin))

    def ecu_census(self, *, ecus: list[dict]) -> None:
        self.emit("ecu_census", ecus=ecus)

    def supported_pids(self, *, ecu: str, pids: list[dict]) -> None:
        self.emit("supported_pids", ecu=ecu, pid_count=len(pids), pids=pids)

    def mil_and_dtc(self, *, ecu: str, mil_status: bool, dtc_count: int) -> None:
        self.emit("mil_and_dtc", ecu=ecu, mil_status=mil_status,
                  dtc_count=dtc_count)

    def mode_06_results(self, *, ecu: str, mids_supported: list[str],
                        tests: list[dict]) -> None:
        self.emit("mode_06_results", ecu=ecu, mids_supported=mids_supported,
                  test_count=len(tests), tests=tests)

    def voltage_read(self, *, voltage_mv: int) -> None:
        self.emit("voltage_read", voltage_mv=voltage_mv,
                  battery_health=battery_health(voltage_mv))

    def connection_complete(self, *, vin: str | None, protocol: str, ecu_count: int,
                            mil_on: bool, stored_dtc_count: int,
                            mode_06_supported: bool,
                            connected_duration_ms: float) -> None:
        self.emit("connection_complete", vin=_partial_vin(vin), protocol=protocol,
                  ecu_count=ecu_count, mil_on=mil_on,
                  stored_dtc_count=stored_dtc_count,
                  mode_06_supported=mode_06_supported,
                  connected_duration_ms=round(connected_duration_ms, 1))

    def voltage_dropped(self, *, voltage_mv: int) -> None:
        self.emit("voltage_dropped", voltage_mv=voltage_mv,
                  status="vehicle_disconnected")

    def channel_closed(self, *, device_id: int) -> None:
        self.emit("channel_closed", device_id=device_id)

    def device_released(self, *, device: str) -> None:
        self.emit("device_released", device=device)

    def device_lost(self, *, device: str) -> None:
        """The held device vanished mid-run (Decision 6: recover to SCANNING)."""
        self.emit("device_lost", device=device, status="device_disconnected")

    def daemon_stop(self, *, uptime_seconds: float, vehicles_logged: int,
                    exit_code: int) -> None:
        # +1 so total_events counts this stop event too (== lines in the file).
        self.emit("daemon_stop", uptime_seconds=round(uptime_seconds, 1),
                  vehicles_logged=vehicles_logged, total_events=self.event_count + 1,
                  exit_code=exit_code)


def _console_line(record: dict) -> str:
    """One concise human line for a headline event."""
    ts = record["timestamp"][11:19]  # HH:MM:SS
    event = record["event"]
    tail = "  ".join(
        f"{k}={v}" for k, v in record.items()
        if k not in ("timestamp", "event") and not isinstance(v, (list, dict))
    )
    return f"[{ts}] {event}  {tail}".rstrip()

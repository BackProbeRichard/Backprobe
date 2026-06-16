"""Probe + census + harvest — drives transport verbs, emits events.

The policy half of the daemon's work (watcher.py owns the lifecycle). Two jobs:

  probe()   — find the connection profile a vehicle actually answers on
              (Decision 4: connect AND confirm with a query; full list twice).
  harvest() — pull VIN, ECU census, per-ECU supported PIDs, MIL/DTC, Mode 06,
              voltage (Decision 5: best-effort — record what we got).

Per-ECU support maps are reconstructed with ask_all over the bitmask groups,
because ask() returns only the first reply. DeviceLost is never caught here: it
propagates so the watcher can release and recover to SCANNING (Decision 6).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from backprobe import session_log
from backprobe.daemon.events import EventLog
from backprobe.obd2 import decode, pids
from backprobe.transport.base import (
    Channel,
    ConnectError,
    ConnectProfile,
    NotSupported,
    Session,
    Timeout,
)

_log = session_log.get_logger("INTERROGATE")

# The probe order (PHASE_1_SCOPE / mockup). Policy lives in the daemon.
PROBE_PROFILES: list[ConnectProfile] = [
    ConnectProfile("ISO15765", 500000, "11-bit"),
    ConnectProfile("ISO15765", 500000, "29-bit"),
    ConnectProfile("ISO15765", 250000, "11-bit"),
    ConnectProfile("ISO15765", 250000, "29-bit"),
]
PROBE_PASSES = 2                     # Decision 4: run the full list twice before giving up
_CONFIRM = b"\x01\x00"               # Mode 01 PID 00 — the "is a vehicle really there?" query
_GROUP_BASES = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0)
_ECU_NAMES = {0x7E8: "Engine", 0x7E9: "Transmission"}  # only standard OBD2 assignments
_ASK_TIMEOUT = 1.0
_CENSUS_WINDOW = 1.0


def _phex(n: int) -> str:
    return f"0x{n:02X}"


def _ehex(n: int) -> str:
    return f"0x{n:X}"


def _ecu_name(addr: int) -> str:
    return _ECU_NAMES.get(addr, f"ECU {_ehex(addr)}")


def protocol_label(p: ConnectProfile) -> str:
    """Human protocol string for connection_complete, e.g.
    'ISO15765 / CAN 11-bit 500kbps'."""
    return f"{p.protocol} / CAN {p.addressing} {p.bitrate // 1000}kbps"


# ─── probe (Decision 4) ─────────────────────────────────────────────────────


@dataclass
class ProbeResult:
    channel: Channel
    profile: ConnectProfile
    step: int


def probe(session: Session, events: EventLog, *,
          profiles: list[ConnectProfile] = PROBE_PROFILES,
          passes: int = PROBE_PASSES) -> ProbeResult | None:
    """Find the profile a vehicle answers on. A profile wins only if connect()
    succeeds AND a confirming 0100 returns a reply; ConnectError or no reply →
    advance. The full list is tried `passes` times before giving up. DeviceLost
    propagates."""
    events.probe_start(attempts=[
        {"step": i, "protocol": p.protocol, "bitrate": p.bitrate,
         "addressing": p.addressing} for i, p in enumerate(profiles, 1)
    ])
    start = time.perf_counter()
    tried = 0
    for _pass in range(passes):
        for step, profile in enumerate(profiles, 1):
            tried += 1
            try:
                channel = session.connect(profile)
            except ConnectError:
                _log.event("PROBE_REJECTED", profile=profile)
                continue
            try:
                channel.ask(_CONFIRM, _ASK_TIMEOUT)        # confirm a vehicle answers
            except (Timeout, NotSupported, decode.DecodeError):
                _log.event("PROBE_NO_REPLY", profile=profile)
                channel.disconnect()
                continue
            elapsed = (time.perf_counter() - start) * 1000
            events.probe_success(protocol=profile.protocol, bitrate=profile.bitrate,
                                 addressing=profile.addressing, winning_step=step,
                                 elapsed_ms=elapsed)
            _log.event("PROBE_SUCCESS", profile=profile, step=step)
            # J2534-specific: if CLEAR_RX_BUFFER failed on this channel, emit to
            # both logs now (Decision 3). VirtualChannel never sets this flag.
            if getattr(channel, "rx_clear_unsupported", False):
                code = getattr(channel, "_rx_clear_code", "UNKNOWN")
                events.rx_clear_unsupported(j2534_code=code)
            return ProbeResult(channel, profile, step)
    events.probe_failed(attempts=tried)
    _log.event("PROBE_FAILED", attempts=tried)
    return None


# ─── harvest (Decision 5) ───────────────────────────────────────────────────


@dataclass
class HarvestSummary:
    """What connection_complete needs, gathered best-effort."""

    vin: str | None
    ecu_count: int
    mil_on: bool | None
    dtc_count: int | None
    mode_06_supported: bool


def _support_walk(channel: Channel, service: int) -> dict[int, list[int]]:
    """Reconstruct each ECU's full support map by walking the bitmask groups
    with ask_all. `service` is 0x01 (PIDs) or 0x06 (MIDs); both decode the same
    (base, supported, more) shape. Returns {ecu_address: [ids]}."""
    decoder = (decode.decode_supported_pids if service == 0x01
               else decode.decode_mode06_mids)
    result: dict[int, list[int]] = {}
    for base in _GROUP_BASES:
        replies = channel.ask_all(bytes([service, base]), _CENSUS_WINDOW)
        if not replies:
            break                                          # nobody (more) here — stop walking
        any_more = False
        for r in replies:
            try:
                _, supported, more = decoder(r.payload)
            except decode.DecodeError:
                continue
            result.setdefault(r.ecu, []).extend(supported)
            any_more = any_more or more
        if not any_more:
            break
    return result


def _pid_list(pid_nums: list[int]) -> list[dict]:
    """[{hex, name, unit}] for the supported_pids event, named via the table."""
    out = []
    for p in sorted(pid_nums):
        entry = pids.PIDS.get(p)
        out.append({"hex": _phex(p),
                    "name": entry.name if entry else None,
                    "unit": entry.unit if entry else ""})
    return out


def _read_vin(channel: Channel, events: EventLog) -> str | None:
    try:
        reply = channel.ask(b"\x09\x02", _ASK_TIMEOUT)
        vin = decode.decode_vin(reply.payload)
    except (Timeout, NotSupported, decode.DecodeError) as exc:
        _log.error("VIN_READ_FAILED", exc)
        return None
    events.vin_read(vin=vin)
    return vin


def _read_mil(channel: Channel, events: EventLog) -> tuple[bool | None, int | None]:
    try:
        reply = channel.ask(b"\x01\x01", _ASK_TIMEOUT)
        mil, count = decode.decode_mil_status(reply.payload)
    except (Timeout, NotSupported, decode.DecodeError) as exc:
        _log.error("MIL_READ_FAILED", exc)
        return None, None
    events.mil_and_dtc(ecu=_ehex(reply.ecu), mil_status=mil, dtc_count=count)
    return mil, count


def _test_dict(t: decode.MonitorTest) -> dict:
    return {"mid": _phex(t.mid), "tid": _phex(t.tid), "description": None,
            "actual": f"{t.value:.3f}", "min": f"{t.min_limit:.3f}",
            "max": f"{t.max_limit:.3f}", "unit": t.unit, "passed": t.passed}


def _harvest_mode06(channel: Channel, events: EventLog, ecus: list[int]) -> bool:
    """Per-ECU Mode 06: MID-support walk, then query each MID and attribute the
    records to the answering ECU. Emits one mode_06_results per ECU."""
    mid_map = _support_walk(channel, 0x06)
    any_tests = False
    for ecu in ecus:
        mids = sorted(mid_map.get(ecu, []))
        tests: list[dict] = []
        for mid in mids:
            for r in channel.ask_all(bytes([0x06, mid]), _CENSUS_WINDOW):
                if r.ecu != ecu:
                    continue
                try:
                    tests.extend(_test_dict(t) for t in decode.decode_mode06(r.payload))
                except decode.DecodeError:
                    continue
        any_tests = any_tests or bool(tests)
        events.mode_06_results(ecu=_ehex(ecu),
                               mids_supported=[_phex(m) for m in mids], tests=tests)
    return any_tests


def harvest(session: Session, channel: Channel, events: EventLog) -> HarvestSummary | None:
    """Best-effort interrogation. Returns a summary, or None if no ECU answers
    the census (Decision 5: <1 ECU is a failed probe, not a connection)."""
    vin = _read_vin(channel, events)

    pid_map = _support_walk(channel, 0x01)                 # the census + per-ECU PID maps
    if not pid_map:
        _log.event("NO_ECU_RESPONSE")
        return None
    ecus = sorted(pid_map)
    events.ecu_census(ecus=[{"address": _ehex(e), "name": _ecu_name(e),
                             "responded": True} for e in ecus])
    for e in ecus:
        events.supported_pids(ecu=_ehex(e), pids=_pid_list(pid_map[e]))

    mil_on, dtc_count = _read_mil(channel, events)
    mode06_supported = _harvest_mode06(channel, events, ecus)

    voltage = session.read_voltage()
    events.voltage_read(voltage_mv=voltage)

    return HarvestSummary(vin=vin, ecu_count=len(ecus), mil_on=mil_on,
                          dtc_count=dtc_count, mode_06_supported=mode06_supported)

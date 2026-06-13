"""OBD2 decode — pure functions, bytes in, meaning out.

Every function takes an OBD response payload exactly as the transport
delivers it (Reply.payload): the service-response byte, the PID/echo, then
the data. Each validates the echo so a mismatched or stale reply is rejected
rather than misread. No I/O, no device, no global state — trivially testable
on any machine with synthetic bytes.

The bitmask *walk* (sending 0x00, 0x20, 0x40, ... in a loop) is orchestration
and lives in the daemon. Here we decode ONE response at a time and report
whether the next group exists; the daemon drives the loop.

What Phase 1's harvest uses:
  decode_supported_pids   — one Mode 01 support bitmask → PID numbers + more?
  decode_mil_status       — Mode 01 PID 01 → (MIL on?, stored DTC count)
  decode_vin              — Mode 09 PID 02 → VIN string
  decode_mode06           — Mode 06 response → list[MonitorTest]
  decode_pid_value        — any Mode 01 PID → Reading (live values; Phase 2)
"""

from __future__ import annotations

from dataclasses import dataclass

from backprobe.obd2 import pids

# Service-response bytes: a positive reply is request mode + 0x40.
_RESP_MODE01 = 0x41
_RESP_MODE06 = 0x46
_RESP_MODE09 = 0x49


class DecodeError(ValueError):
    """A response could not be decoded (wrong echo, too short, malformed)."""


# ─── result carriers ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reading:
    """One decoded Mode 01 PID. value is None when the PID is multi-field
    (named for the census, no single scalar) — raw bytes are always kept."""

    pid: int
    name: str | None      # None → not a known standard PID
    value: float | int | str | None
    unit: str
    raw: bytes


@dataclass(frozen=True)
class MonitorTest:
    """One Mode 06 on-board test result (a MID/TID record)."""

    mid: int
    tid: int
    value: float
    min_limit: float
    max_limit: float
    unit: str
    passed: bool


# ─── Mode 01: supported-PID bitmask (one group) ────────────────────────────


def decode_supported_pids(response: bytes) -> tuple[int, list[int], bool]:
    """Decode one Mode 01 support bitmask (PID 0x00/0x20/0x40/...).

    Returns (base, supported_pids, more_groups):
      base          — the group base this response answered (0x00, 0x20, ...)
      supported_pids— PID numbers flagged supported in this group
      more_groups   — True if the next group's support PID is itself supported
                      (the daemon uses this to decide whether to keep walking)

    Wire: [0x41][base][A][B][C][D]. The 32-bit mask A..D: bit 31 = base+1,
    down to bit 0 = base+32. Bit 0 doubles as "next group present".
    """
    if len(response) < 6 or response[0] != _RESP_MODE01:
        raise DecodeError(f"not a Mode 01 support reply: {response.hex()}")
    base = response[1]
    mask = int.from_bytes(response[2:6], "big")
    supported = [base + k for k in range(1, 32) if mask & (1 << (32 - k))]
    more_groups = bool(mask & 0x01)
    return base, supported, more_groups


# ─── Mode 01 PID 01: MIL + stored DTC count ────────────────────────────────


def decode_mil_status(response: bytes) -> tuple[bool, int]:
    """Decode Mode 01 PID 01 → (MIL illuminated?, stored DTC count).

    Wire: [0x41][0x01][A][B][C][D]. Byte A bit 7 = MIL lamp; bits 6..0 = count.
    """
    if len(response) < 3 or response[0] != _RESP_MODE01 or response[1] != 0x01:
        raise DecodeError(f"not a Mode 01 PID 01 reply: {response.hex()}")
    a = response[2]
    return bool(a & 0x80), a & 0x7F


# ─── Mode 09 PID 02: VIN ───────────────────────────────────────────────────


def decode_vin(response: bytes) -> str:
    """Decode Mode 09 PID 02 → VIN string (ISO-TP reassembled by the DLL).

    Wire: [0x49][0x02][count][17 ASCII bytes]. The count byte (number of data
    items) is skipped; trailing NULs/padding are stripped.
    """
    if len(response) < 3 or response[0] != _RESP_MODE09 or response[1] != 0x02:
        raise DecodeError(f"not a Mode 09 PID 02 reply: {response.hex()}")
    raw = response[3:]
    vin = raw.decode("ascii", errors="replace").rstrip("\x00 ").strip()
    return vin


# ─── Mode 01: any PID → Reading (live values; exercised in Phase 2) ────────


def decode_pid_value(response: bytes) -> Reading:
    """Decode a Mode 01 current-data reply into a Reading.

    Looks the PID up in the table and applies its decode rule. An unknown PID
    or a multi-field one (decode=None) yields a Reading with value=None but
    keeps name (if known), unit, and raw bytes — nothing is hidden.
    """
    if len(response) < 2 or response[0] != _RESP_MODE01:
        raise DecodeError(f"not a Mode 01 reply: {response.hex()}")
    pid = response[1]
    data = response[2:]
    entry = pids.PIDS.get(pid)
    if entry is None:
        return Reading(pid=pid, name=None, value=None, unit="", raw=data)
    value: float | int | str | None = None
    if entry.decode is not None and len(data) >= entry.n_bytes:
        value = entry.decode(data)
    return Reading(pid=pid, name=entry.name, value=value, unit=entry.unit, raw=data)


# ─── Mode 06: on-board monitor test results ────────────────────────────────
# SAE J1979 Table — Unit and Scaling IDs (UASID): scaling_id → (factor, unit).
# A raw 16-bit test value is multiplied by factor to get engineering units.

_MODE06_SCALING: dict[int, tuple[float, str]] = {
    0x01: (1.0, ""),          # raw / unitless
    0x02: (0.1, ""),
    0x03: (0.01, ""),
    0x04: (0.001, ""),
    0x05: (0.0000305, ""),    # 2 / 65536
    0x06: (0.000122, ""),     # 8 / 65536
    0x07: (0.25, "rpm"),
    0x08: (0.01, "km/h"),
    0x09: (1.0, "km/h"),
    0x0A: (0.122, "mV"),
    0x0B: (0.001, "V"),
    0x0C: (0.01, "V"),
    0x0D: (0.00390625, "mA"),
    0x0E: (0.001, "A"),
    0x0F: (1.0, "ms"),
    0x10: (100.0, "ms"),
    0x11: (1.0, "s"),
    0x12: (0.1, "s"),
    0x13: (1.0, "Ω"),
    0x14: (1000.0, "Ω"),      # kΩ in raw → Ω
    0x15: (0.001, "kPa"),
    0x16: (0.0078125, "kPa"),
    0x17: (0.1, "kPa"),
    0x18: (0.01, "kPa"),
    0x19: (1.0, "kPa"),
    0x1A: (0.1, "°C"),        # 0.1 °C/bit, then offset (see below)
    0x1B: (1.0, "%"),
    0x1C: (100.0 / 255, "%"),
    0x1D: (0.01, "%"),
    0x1E: (0.0000305, "%"),   # 2 / 65536
    0x1F: (1.0, "count"),
}

# Scaling IDs that carry a -40 °C offset after the factor (temperatures).
_MODE06_OFFSET_TEMP = {0x1A}


def _scale_mode06(scaling_id: int, raw: int) -> tuple[float, str]:
    """Raw 16-bit Mode 06 value → (engineering value, unit). Unknown IDs pass
    the raw integer through with the scaling id named, so data is never lost."""
    factor, unit = _MODE06_SCALING.get(scaling_id, (1.0, f"scale_{scaling_id:#04x}"))
    value = raw * factor
    if scaling_id in _MODE06_OFFSET_TEMP:
        value -= 40
    return value, unit


def decode_mode06(response: bytes) -> list[MonitorTest]:
    """Decode a Mode 06 reply into MID/TID test records.

    Wire (SAE J1979 §6.6): [0x46] then repeating 9-byte records:
      [MID][TID][UASID][VAL_H][VAL_L][MIN_H][MIN_L][MAX_H][MAX_L]
    Each record carries its own MID; one reply may span several. Pass/fail is
    min ≤ value ≤ max on the raw integers (before scaling).
    """
    if len(response) < 10 or response[0] != _RESP_MODE06:
        raise DecodeError(f"not a Mode 06 reply: {response.hex()}")
    tests: list[MonitorTest] = []
    i = 1
    while i + 8 < len(response):
        mid = response[i]
        tid = response[i + 1]
        uasid = response[i + 2]
        raw_val = response[i + 3] << 8 | response[i + 4]
        raw_min = response[i + 5] << 8 | response[i + 6]
        raw_max = response[i + 7] << 8 | response[i + 8]

        value, unit = _scale_mode06(uasid, raw_val)
        min_v, _ = _scale_mode06(uasid, raw_min)
        max_v, _ = _scale_mode06(uasid, raw_max)

        tests.append(
            MonitorTest(
                mid=mid, tid=tid, value=value, min_limit=min_v, max_limit=max_v,
                unit=unit, passed=raw_min <= raw_val <= raw_max,
            )
        )
        i += 9
    return tests


def decode_mode06_mids(response: bytes) -> tuple[int, list[int], bool]:
    """Decode a Mode 06 MID-support bitmask (MID 0x00/0x20/...) — same shape
    as the Mode 01 support walk. Returns (base, supported_mids, more_groups)."""
    if len(response) < 6 or response[0] != _RESP_MODE06:
        raise DecodeError(f"not a Mode 06 support reply: {response.hex()}")
    base = response[1]
    mask = int.from_bytes(response[2:6], "big")
    supported = [base + k for k in range(1, 32) if mask & (1 << (32 - k))]
    return base, supported, bool(mask & 0x01)

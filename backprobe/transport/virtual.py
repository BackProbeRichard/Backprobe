"""Virtual transport backend — a fake vehicle in pure Python, no hardware.

Implements the same `base.py` interface as the J2534 backend so the daemon
(build-order step 6) can be built and tested entirely on the Linux dev box. The
guiding principle (PHASE_1_VIRTUAL_DESIGN.md): the more honestly it fakes a
vehicle, the more the daemon is actually tested.

Design decisions (all committed as Option A):
  1. Vehicle = a data PROFILE (VIN, ECUs, supported PIDs, DTCs, Mode 06);
     wire bytes are synthesized from it on demand (encoders below).
  2. Plug-in is programmatic: VirtualSession.plug_in(vehicle) / unplug().
  3. connect() rejects profile mismatches, so the daemon's probe loop is
     genuinely exercised — a wrong profile fails, the loop advances.
  4. The byte encoders live here, local to the only thing that fakes a vehicle.
  5. Instant by default; an optional latency_ms knob adds realism for demos.

The presets are seeded from real OldVer captures (the supported-PID masks, MIL
bytes, and Mode 06 records are real, non-PII wire data); VINs are synthetic —
real VINs are PII and never belong in tracked code (see session_log tokenizing).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from backprobe import session_log
from backprobe.obd2 import pids
from backprobe.transport.base import (
    Channel,
    ConnectError,
    ConnectProfile,
    Device,
    DeviceBusy,
    DeviceLost,
    DeviceReport,
    Reply,
    Session,
    Timeout,
    TransportBackend,
)

# ─── vehicle data model (Decision 1) ───────────────────────────────────────


@dataclass(frozen=True)
class Mode06Record:
    """One Mode 06 on-board test result as raw 16-bit values, exactly as it
    sits on the wire before scaling. decode.decode_mode06 scales these."""

    mid: int
    tid: int
    uasid: int
    value: int
    min_limit: int
    max_limit: int


@dataclass
class VirtualECU:
    """One ECU on the bus: its CAN response address and everything it answers.

    supported_pids holds Mode 01 *data* PID numbers (not the structural group
    PIDs 0x00/0x20/…) — the support bitmask is synthesized from this set.
    pid_values maps a PID to its raw live-data payload (the bytes after the
    PID echo); PID 0x01 (MIL/DTC) is built from mil_on/dtc_count/readiness.
    """

    address: int                                    # response CAN ID, e.g. 0x7E8
    supported_pids: set[int] = field(default_factory=set)
    pid_values: dict[int, bytes] = field(default_factory=dict)
    mil_on: bool = False
    dtc_count: int = 0
    readiness: bytes = b"\x07\xe1\x00"              # PID 01 bytes B,C,D (monitor readiness)
    vin: str | None = None                          # only the main ECU answers Mode 09
    mode06: list[Mode06Record] = field(default_factory=list)

    @property
    def supported_mids(self) -> set[int]:
        """Mode 06 MIDs this ECU reports tests for (drives the MID-support walk)."""
        return {r.mid for r in self.mode06}


@dataclass
class VirtualVehicle:
    """A fake vehicle: how it connects, plus the ECUs that answer on its bus."""

    name: str
    profile: ConnectProfile                         # the CAN config it actually speaks
    ecus: list[VirtualECU]

    @property
    def main_ecu(self) -> VirtualECU:
        """The primary responder (engine/PCM) — the VIN- and MIL-bearing ECU."""
        return self.ecus[0]


# ─── presets, seeded from real OldVer captures ─────────────────────────────
# PID sets, MIL bytes, and Mode 06 records are decoded from real logs; the
# encoders below reproduce the original wire bytes exactly (the round-trip
# anchor). VINs are synthetic but structurally valid (charset excludes I/O/Q).

# Vehicle A (session_20260606): MIL ON, 2 DTCs, 3 PID groups, one Mode 06 MID.
_VEHICLE_A = VirtualVehicle(
    name="sedan_mil_on",
    profile=ConnectProfile(protocol="ISO15765", bitrate=500000, addressing="11-bit"),
    ecus=[
        VirtualECU(
            address=0x7E8,
            supported_pids={0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D,
                            0x0E, 0x0F, 0x10, 0x11, 0x1F, 0x2F, 0x51, 0x5C},
            mil_on=True,
            dtc_count=2,
            readiness=b"\x07\xe1\x00",              # real PID01 = 82 07 e1 00
            vin="1HGCM82633A004321",
            mode06=[
                Mode06Record(0x01, 0x0B, 0x0C, 0x0034, 0x000A, 0x00C8),
                Mode06Record(0x01, 0x0C, 0x0C, 0x0041, 0x000A, 0x00C8),
            ],
            pid_values={
                0x0C: b"\x0c\x80",                  # rpm = 800
                0x0D: b"\x00",                      # speed = 0 km/h (idling)
                0x05: b"\x5a",                      # coolant = 50 °C
            },
        ),
    ],
)

# Vehicle B (session_20260607): MIL OFF, 0 DTCs, 6 PID groups (rich coverage).
_VEHICLE_B = VirtualVehicle(
    name="truck_healthy",
    profile=ConnectProfile(protocol="ISO15765", bitrate=500000, addressing="11-bit"),
    ecus=[
        VirtualECU(
            address=0x7E8,
            supported_pids={0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0C,
                            0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x13, 0x15, 0x19, 0x1C,
                            0x1F, 0x21, 0x24, 0x28, 0x2E, 0x2F, 0x30, 0x31, 0x33,
                            0x34, 0x38, 0x3C, 0x3D, 0x3E, 0x3F, 0x41, 0x42, 0x43,
                            0x44, 0x45, 0x46, 0x47, 0x49, 0x4A, 0x4C, 0x4D, 0x4E,
                            0x51, 0x53, 0x55, 0x56, 0x57, 0x58, 0x62, 0x63, 0x65,
                            0x6D, 0x8E, 0x9D, 0x9E, 0xA6},
            mil_on=False,
            dtc_count=0,
            readiness=b"\x07\xe5\x21",              # real PID01 = 00 07 e5 21
            vin="5TFUY5F1XBX556677",
            mode06=[                                 # real, conformant 0601 (5 records, MID 1)
                Mode06Record(0x01, 0x91, 0x8D, 0x0196, 0x006E, 0x022E),
                Mode06Record(0x01, 0x93, 0x8A, 0x0645, 0x01C4, 0x7FFF),
                Mode06Record(0x01, 0x94, 0x8A, 0xFB01, 0x8000, 0xFE15),
                Mode06Record(0x01, 0x95, 0x90, 0x0010, 0x0000, 0x0167),
                Mode06Record(0x01, 0x96, 0x90, 0x0000, 0x0000, 0x0167),
            ],
            pid_values={
                0x0C: b"\x1a\xf0",                  # rpm = 1724
                0x0D: b"\x41",                      # speed = 65 km/h
                0x05: b"\x5e",                      # coolant = 54 °C
            },
        ),
        # Synthetic second ECU (transmission, real 0x7E9 address) so the
        # multi-ECU census (acceptance test #3) has more than one responder.
        VirtualECU(
            address=0x7E9,
            supported_pids={0x01, 0x0D, 0xA4},
            mil_on=False,
            dtc_count=0,
            pid_values={0x0D: b"\x41", 0xA4: b"\x00\x00\x03\xe8"},
        ),
    ],
)

# Vehicle C (session_20260608 data): a 29-bit vehicle, so connect() must reject
# the 11-bit profiles the daemon tries first and the probe loop must advance.
_VEHICLE_C = VirtualVehicle(
    name="diesel_29bit",
    profile=ConnectProfile(protocol="ISO15765", bitrate=500000, addressing="29-bit"),
    ecus=[
        VirtualECU(
            address=0x18DAF110,
            supported_pids={0x01, 0x03, 0x04, 0x05, 0x0C, 0x0D, 0x1F, 0x21, 0x31},
            mil_on=True,
            dtc_count=1,
            readiness=b"\x07\xe5\x00",              # real PID01 = 81 07 e5 00
            vin="2GCEK19T8X1100099",
        ),
    ],
)

PRESETS: dict[str, VirtualVehicle] = {
    v.name: v for v in (_VEHICLE_A, _VEHICLE_B, _VEHICLE_C)
}


def get_preset(name: str) -> VirtualVehicle:
    """Look up a preset vehicle by name. Raises KeyError if unknown."""
    return PRESETS[name]


# ─── byte encoders (Decision 4: local to virtual.py) ───────────────────────
# The inverse of obd2/decode.py: meaning → wire bytes. Each produces a payload
# in the exact shape decode.py expects (service-response byte, echo, data), so
# decode(encode(x)) == x. Anchored in tests to real OldVer bytes.

_RESP_MODE01 = 0x41
_RESP_MODE06 = 0x46
_RESP_MODE09 = 0x49


def _encode_support_mask(resp_byte: int, base: int, supported: set[int]) -> bytes:
    """Encode a 32-bit support bitmask group → [resp, base, A, B, C, D].

    Shared by Mode 01 PID support and Mode 06 MID support (same shape, different
    response byte). Bit (32-k) flags id base+k for k in 1..31; bit 0 ("more")
    is set when any supported id lives in a higher group. Inverse of
    decode.decode_supported_pids / decode.decode_mode06_mids.
    """
    mask = 0
    for s in supported:
        k = s - base
        if 1 <= k <= 31:
            mask |= 1 << (32 - k)
    if any(s > base + 0x1F for s in supported):     # a higher group has content
        mask |= 0x01
    return bytes([resp_byte, base]) + mask.to_bytes(4, "big")


def _encode_pid01(mil_on: bool, dtc_count: int, readiness: bytes) -> bytes:
    """Encode Mode 01 PID 01 → [0x41, 0x01, A, B, C, D]. A bit 7 = MIL lamp,
    bits 6..0 = stored DTC count; B/C/D carry monitor readiness. Inverse of
    decode.decode_mil_status."""
    a = (0x80 if mil_on else 0x00) | (dtc_count & 0x7F)
    return bytes([_RESP_MODE01, 0x01, a]) + readiness


def _encode_pid_value(pid: int, data: bytes) -> bytes:
    """Encode a Mode 01 live-data reply → [0x41, pid, *data]. Inverse of
    decode.decode_pid_value."""
    return bytes([_RESP_MODE01, pid]) + data


def _encode_vin(vin: str) -> bytes:
    """Encode Mode 09 PID 02 → [0x49, 0x02, 0x01, *17 ASCII]. The 0x01 is the
    data-item count. Inverse of decode.decode_vin."""
    raw = vin.encode("ascii")
    if len(raw) != 17:
        raise ValueError(f"VIN must be 17 chars, got {len(raw)}: {vin!r}")
    return bytes([_RESP_MODE09, 0x02, 0x01]) + raw


def _encode_mode06(records: list[Mode06Record]) -> bytes:
    """Encode Mode 06 test records → [0x46, then 9 bytes per record]. Inverse of
    decode.decode_mode06."""
    out = bytearray([_RESP_MODE06])
    for r in records:
        out += bytes([r.mid, r.tid, r.uasid,
                      r.value >> 8, r.value & 0xFF,
                      r.min_limit >> 8, r.min_limit & 0xFF,
                      r.max_limit >> 8, r.max_limit & 0xFF])
    return bytes(out)


_SUPPORT_BASES = {0x00, 0x20, 0x40, 0x60, 0x80, 0xA0}


def _respond(ecu: VirtualECU, request: bytes) -> bytes | None:
    """One ECU's answer to one request, or None if it stays silent (which the
    channel surfaces as a Timeout on ask, or just absence on ask_all). This is
    the heart of the fake: it routes a [mode][id] request to the right encoder.
    """
    if len(request) < 2:
        return None
    mode, ident = request[0], request[1]

    if mode == 0x01:                                       # current data
        if ident in _SUPPORT_BASES:
            if ident != 0x00 and not any(p > ident for p in ecu.supported_pids):
                return None                                # nothing here or beyond
            return _encode_support_mask(_RESP_MODE01, ident, ecu.supported_pids)
        if ident not in ecu.supported_pids:
            return None
        if ident == 0x01:                                  # MIL / DTC count
            return _encode_pid01(ecu.mil_on, ecu.dtc_count, ecu.readiness)
        data = ecu.pid_values.get(ident)
        if data is None:                                   # supported but no live value set
            entry = pids.PIDS.get(ident)
            data = bytes(entry.n_bytes) if entry else b"\x00"
        return _encode_pid_value(ident, data)

    if mode == 0x06:                                       # on-board monitor tests
        if ident in _SUPPORT_BASES:
            if ident != 0x00 and not any(m > ident for m in ecu.supported_mids):
                return None
            return _encode_support_mask(_RESP_MODE06, ident, ecu.supported_mids)
        records = [r for r in ecu.mode06 if r.mid == ident]
        return _encode_mode06(records) if records else None

    if mode == 0x09:                                       # vehicle information
        if ident == 0x00:
            return _encode_support_mask(_RESP_MODE09, 0x00, {0x02} if ecu.vin else set())
        if ident == 0x02 and ecu.vin:
            return _encode_vin(ecu.vin)
        return None

    return None                                            # unsupported mode


# ─── the transport objects (base.py interface) ─────────────────────────────

_FUNCTIONAL_TX = {"11-bit": 0x7DF, "29-bit": 0x18DB33F1}   # for log readability only

_DEVICE = Device(vendor="Backprobe", name="Virtual Pass-Thru", address="virtual://0")


class VirtualBackend(TransportBackend):
    """A fake J2534-shaped backend. `busy` simulates another app owning the
    device (DeviceBusy on open); `attached`/`latency_ms` seed the session it
    opens, so the simplest demo is VirtualBackend(attached=get_preset(...))."""

    def __init__(self, *, attached: VirtualVehicle | None = None,
                 busy: bool = False, latency_ms: float = 0.0) -> None:
        self._attached = attached
        self._busy = busy
        self._latency_ms = latency_ms
        self._log = session_log.get_logger("VIRTUAL")

    def enumerate(self) -> list[Device]:
        """One virtual device, always present. Never raises."""
        return [_DEVICE]

    def open(self, device: Device) -> "VirtualSession":
        if self._busy:
            self._log.event("OPEN_REJECTED", device=device.name, reason="busy")
            raise DeviceBusy("virtual device is held by another program")
        self._log.event("OPEN", device=device.name)
        return VirtualSession(attached=self._attached, latency_ms=self._latency_ms)


class VirtualSession(Session):
    """One opened virtual device. Holds at most one attached vehicle; plug_in /
    unplug drive the presence signal read_voltage reports (Decision 2)."""

    _V_ATTACHED = 12_600     # mV, engine off / key on
    _V_UNPLUGGED = 200       # mV, no vehicle

    def __init__(self, *, attached: VirtualVehicle | None = None,
                 latency_ms: float = 0.0) -> None:
        self._vehicle = attached
        self._latency_ms = latency_ms
        self._closed = False
        self._log = session_log.get_logger("VIRTUAL")

    # — test/script controls (the "cable", Decision 2A) —
    def plug_in(self, vehicle: VirtualVehicle) -> None:
        """Simulate connecting a vehicle: voltage rises, probes can now connect."""
        self._vehicle = vehicle
        self._log.event("PLUG_IN", vehicle=vehicle.name, profile=vehicle.profile)

    def unplug(self) -> None:
        """Simulate pulling the cable: voltage drops back to ~0."""
        name = self._vehicle.name if self._vehicle else None
        self._vehicle = None
        self._log.event("UNPLUG", vehicle=name)

    # — Session interface —
    def info(self) -> DeviceReport:
        return DeviceReport(
            vendor=_DEVICE.vendor, name=_DEVICE.name, address=_DEVICE.address,
            firmware="virtual-1.0", dll_version="virtual-1.0", api_version="04.04",
        )

    def read_voltage(self) -> int:
        if self._closed:
            raise DeviceLost("session closed")
        return self._V_ATTACHED if self._vehicle else self._V_UNPLUGGED

    def connect(self, profile: ConnectProfile) -> "VirtualChannel":
        if self._closed:
            raise DeviceLost("session closed")
        if self._vehicle is None:
            raise ConnectError("no vehicle attached")
        if profile != self._vehicle.profile:                # Decision 3: reject mismatches
            self._log.event("CONNECT_REJECTED", tried=profile, want=self._vehicle.profile)
            raise ConnectError(f"profile {profile} does not match vehicle")
        self._log.event("CONNECT", profile=profile, vehicle=self._vehicle.name)
        return VirtualChannel(self._vehicle, self._latency_ms)

    def close(self) -> None:
        """Idempotent, never raises — releases the (fake) device completely."""
        if not self._closed:
            self._closed = True
            self._log.event("CLOSE")


class VirtualChannel(Channel):
    """A live connection to a fake vehicle. ask routes to the first ECU that
    answers; ask_all is the census (every answering ECU)."""

    def __init__(self, vehicle: VirtualVehicle, latency_ms: float = 0.0) -> None:
        self._vehicle = vehicle
        self._latency_ms = latency_ms
        self._disconnected = False
        self._tx = _FUNCTIONAL_TX.get(vehicle.profile.addressing, 0)
        self._log = session_log.get_logger("VIRTUAL")

    def _settle(self) -> None:
        if self._latency_ms > 0:
            time.sleep(self._latency_ms / 1000.0)

    def ask(self, request: bytes, timeout: float) -> Reply:
        if self._disconnected:
            raise DeviceLost("channel disconnected")
        start = time.perf_counter()
        self._settle()
        for ecu in self._vehicle.ecus:
            payload = _respond(ecu, request)
            if payload is not None:
                reply = Reply(ecu=ecu.address, payload=payload)
                self._log.exchange(self._tx, request, [(ecu.address, payload)],
                                   (time.perf_counter() - start) * 1000)
                return reply
        elapsed = (time.perf_counter() - start) * 1000
        self._log.exchange(self._tx, request, [], elapsed)
        raise Timeout(f"no reply to {request.hex()} within {timeout}s")

    def ask_all(self, request: bytes, window: float) -> list[Reply]:
        if self._disconnected:
            raise DeviceLost("channel disconnected")
        start = time.perf_counter()
        self._settle()
        replies = [Reply(ecu=ecu.address, payload=p)
                   for ecu in self._vehicle.ecus
                   if (p := _respond(ecu, request)) is not None]
        self._log.exchange(self._tx, request,
                           [(r.ecu, r.payload) for r in replies],
                           (time.perf_counter() - start) * 1000)
        return replies

    def disconnect(self) -> None:
        """Tear down the connection, keep the (fake) device held. Idempotent."""
        if not self._disconnected:
            self._disconnected = True
            self._log.event("DISCONNECT")

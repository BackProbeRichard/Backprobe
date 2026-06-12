"""Virtual (mock) transport for offline testing and development.

Simulates a connected vehicle with configurable PID responses and DTC sets.
Also emits realistic CAN broadcast frames on a python-can virtual bus so the
CanSnifferProtocol can be tested without physical hardware.

Simulated broadcast messages use frame IDs and signal layouts that match
the companion test DBC at tests/fixtures/virtual_vehicle.dbc.
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from typing import Any

import can

from obd2_mcp.transport.base import BaseTransport, TransportStatus

# ---------------------------------------------------------------------------
# Simulated UDS/OBD-II response table (Mode 01 PIDs -> raw response bytes)
# ---------------------------------------------------------------------------
_SIMULATED_PIDS: dict[int, bytes] = {
    # ── Mode 01 bitmask PIDs (4-byte response indicates which PIDs are supported) ─
    # Group 0x00: PIDs 0x01–0x1F + 0x20 group indicator
    # Supported: 0x01,0x03–0x07,0x0B–0x11,0x1F + next group (0x20)
    # Bitmask 0xBE3F8003 → bytes 0xBE, 0x3F, 0x80, 0x03
    0x00: bytes([0x41, 0x00, 0xBE, 0x3F, 0x80, 0x03]),
    # Group 0x20: PIDs 0x21–0x3F + 0x40 indicator → 0x2F + next group
    0x20: bytes([0x41, 0x20, 0x00, 0x02, 0x00, 0x01]),
    # Group 0x40: PIDs 0x41–0x5F → 0x51, 0x5C (no next group)
    0x40: bytes([0x41, 0x40, 0x00, 0x00, 0x80, 0x10]),
    # ── Actual Mode 01 PID responses ─────────────────────────────────────────────
    0x01: bytes([0x41, 0x01, 0x82, 0x07, 0xE1, 0x00]),  # Monitor status: MIL on, 2 DTCs
    0x03: bytes([0x41, 0x03, 0x02, 0x00]),               # Fuel system: closed loop B1, B2 N/A
    0x04: bytes([0x41, 0x04, 0x28]),                     # Engine load: 16%
    0x05: bytes([0x41, 0x05, 0x64]),                     # Coolant temp: 60°C
    0x06: bytes([0x41, 0x06, 0x80]),                     # STFT B1: 0%
    0x07: bytes([0x41, 0x07, 0x7A]),                     # LTFT B1: -4.7%
    0x0B: bytes([0x41, 0x0B, 0x65]),                     # MAP: 101 kPa
    0x0C: bytes([0x41, 0x0C, 0x0F, 0xA0]),               # RPM: 1000 rpm
    0x0D: bytes([0x41, 0x0D, 0x32]),                     # Speed: 50 km/h
    0x0E: bytes([0x41, 0x0E, 0x84]),                     # Timing advance: 2° bTDC
    0x0F: bytes([0x41, 0x0F, 0x3C]),                     # Intake air temp: 20°C
    0x10: bytes([0x41, 0x10, 0x04, 0x4C]),               # MAF: 10.88 g/s
    0x11: bytes([0x41, 0x11, 0x3D]),                     # Throttle: 23.9%
    0x1F: bytes([0x41, 0x1F, 0x01, 0x2C]),               # Engine run time: 300 s
    0x2F: bytes([0x41, 0x2F, 0xC8]),                     # Fuel level: 78.4%
    0x51: bytes([0x41, 0x51, 0x01]),                     # Fuel type: Gasoline
    0x5C: bytes([0x41, 0x5C, 0x6E]),                     # Oil temp: 70°C (0x6E-40)
}

# ── Freeze frame (Mode 02) — values captured at P0300 fault moment ──────────
_FREEZE_FRAME_PIDS: dict[int, bytes] = {
    0x04: bytes([0x42, 0x04, 0x00, 0x4A]),               # Engine load: 28.6% (higher at fault)
    0x05: bytes([0x42, 0x05, 0x00, 0x50]),               # Coolant temp: 40°C (cold at fault)
    0x0B: bytes([0x42, 0x0B, 0x00, 0x62]),               # MAP: 98 kPa
    0x0C: bytes([0x42, 0x0C, 0x00, 0x12, 0x8C]),         # RPM: 1187 rpm
    0x0D: bytes([0x42, 0x0D, 0x00, 0x00]),               # Speed: 0 km/h (idle at fault)
    0x0E: bytes([0x42, 0x0E, 0x00, 0x80]),               # Timing advance: 0°
    0x0F: bytes([0x42, 0x0F, 0x00, 0x3A]),               # IAT: 18°C
    0x10: bytes([0x42, 0x10, 0x00, 0x03, 0x84]),         # MAF: 9.0 g/s
    0x11: bytes([0x42, 0x11, 0x00, 0x28]),               # Throttle: 15.7%
    0x1F: bytes([0x42, 0x1F, 0x00, 0x00, 0x3C]),         # Run time: 60 s (just started)
    0x2F: bytes([0x42, 0x2F, 0x00, 0xC8]),               # Fuel level: 78.4%
}

# ── Mode 06 MID support bitmasks (same walk pattern as Mode 01 PID bitmasks) ─
# Group 0x00: MID 0x01 supported (k=1→bit31) + next group (k=32→bit0) = 0x80000001
# Group 0x20: MID 0x21 supported (k=1→bit31) + next group (k=32→bit0) = 0x80000001
# Group 0x40: MID 0x41 supported (k=1→bit31), no next group              = 0x80000000
_MODE06_MID_BITMASKS: dict[int, bytes] = {
    0x00: bytes([0x46, 0x00, 0x80, 0x00, 0x00, 0x01]),
    0x20: bytes([0x46, 0x20, 0x80, 0x00, 0x00, 0x01]),
    0x40: bytes([0x46, 0x40, 0x80, 0x00, 0x00, 0x00]),
}

# ── Mode 06 per-MID responses ─────────────────────────────────────────────────
# Correct wire format (SAE J1979): 0x46 then repeating 9-byte records, each
# carrying its own MID: [MID, TID, SCALING, VAL_H, VAL_L, MIN_H, MIN_L, MAX_H, MAX_L]
#
# MID 0x01 — O2 sensor bank 1 (2 TIDs)
#   TID 0x0B: B1S1 heater resistance, scaling 0x0C (1Ω/bit)
#             Actual=52Ω, Min=10Ω, Max=200Ω → PASS
#   TID 0x0C: B1S2 heater resistance, same scaling
#             Actual=65Ω, Min=10Ω, Max=200Ω → PASS
#
# MID 0x21 — Catalyst efficiency bank 1
#   TID 0x02: efficiency, scaling 0x81 (0.01%/bit)
#             Actual=90→0.90, Min=60→0.60, Max=100→1.00 → PASS
#
# MID 0x41 — EVAP purge flow
#   TID 0x04: purge flow, scaling 0x09 (0.390625%/bit)
#             Actual=5→1.95%, Min=10→3.91%, Max=200→78.1% → FAIL
_MODE06_MID_RESPONSES: dict[int, bytes] = {
    0x01: bytes([
        0x46,
        0x01, 0x0B, 0x0C, 0x00, 52,  0x00, 10,  0x00, 200,
        0x01, 0x0C, 0x0C, 0x00, 65,  0x00, 10,  0x00, 200,
    ]),
    0x21: bytes([
        0x46,
        0x21, 0x02, 0x81, 0x00, 90,  0x00, 60,  0x00, 100,
    ]),
    0x41: bytes([
        0x46,
        0x41, 0x04, 0x09, 0x00, 5,   0x00, 10,  0x00, 200,
    ]),
}

# ── Mode 09 vehicle info responses ──────────────────────────────────────────
_VIN_ASCII = b"1GTFW1ET5EFA00001"  # 17 bytes
_CAL_ID    = b"68359359AL      "   # 16 bytes (padded)
_CVN_BYTES = bytes([0x12, 0x34, 0x56, 0x78])
_ECU_NAME  = b"ENGINE CTRL MOD     "  # 20 bytes (padded)

_SIMULATED_DTCS: list[str] = ["P0300", "P0420"]

# ---------------------------------------------------------------------------
# Simulated broadcast CAN frame builders
#
# Each function takes elapsed time t (seconds) and returns 8 data bytes.
# Frame IDs match virtual_vehicle.dbc:
#   0x200 = ECM_Engine_Data
#   0x210 = ECM_AC_Data
#   0x350 = HVAC_Status
#   0x400 = BCM_Body_Status
#   0x500 = TCM_Status
# ---------------------------------------------------------------------------

def _ecm_engine_data(t: float) -> bytes:
    """ECM broadcast: RPM (oscillating 800-1000), coolant, throttle, load."""
    rpm = int(800 + 200 * math.sin(t * 0.5)) & 0xFFFF
    coolant = 80 & 0xFF
    throttle = int(15 + 5 * math.sin(t * 0.3)) & 0xFF
    load = int(20 + 5 * math.sin(t * 0.4)) & 0xFF
    return struct.pack(">HBBBB", rpm, coolant, throttle, load, 0x00, 0x00)


def _ecm_ac_data(t: float) -> bytes:
    """ECM A/C broadcast: pressure sensor, A/C request, compressor command.

    Simulates A/C pressure dropping after 30s (compressor cycling off).
    """
    pressure_raw = int(150 - 30 * (1 if t > 30 else 0)) & 0xFF
    ac_request = 0x01
    compressor_cmd = 0x01 if t < 30 or (t % 20) < 10 else 0x00
    return struct.pack(">BBBBBBB", pressure_raw, ac_request, compressor_cmd,
                       0x00, 0x00, 0x00, 0x00)


def _hvac_status(t: float) -> bytes:
    """HVAC module broadcast: A/C permission, blend door, zone temp, fan speed.

    A/C permission drops ~2 seconds AFTER ECM pressure drops (at t ~= 32).
    This intentional lag is the key cross-module correlation test case.
    """
    ac_permission = 0x01 if t < 32 else 0x00
    blend_door = 0x7F
    zone_temp = 22 & 0xFF
    fan_speed = 0x03
    return struct.pack(">BBBBBBBB", ac_permission, blend_door, zone_temp,
                       fan_speed, 0x00, 0x00, 0x00, 0x00)


def _bcm_body_status(t: float) -> bytes:
    """BCM broadcast: A/C relay state, fan relay.

    Relay drops ~1s after HVAC permission (at t ~= 33).
    """
    ac_relay = 0x01 if t < 33 else 0x00
    fan_relay = 0x01
    return struct.pack(">BBBBBBBB", ac_relay, fan_relay,
                       0x00, 0x00, 0x00, 0x00, 0x00, 0x00)


def _tcm_status(t: float) -> bytes:
    """TCM broadcast: gear, TCC state, output speed."""
    gear = 0x04
    tcc = 0x01
    output_speed = int(800 + 100 * math.sin(t * 0.2)) & 0xFFFF
    return struct.pack(">BBH4x", gear, tcc, output_speed)


# (arb_id, period_seconds, builder_fn)
_BROADCAST_SCHEDULE: list[tuple[int, float, Any]] = [
    (0x200, 0.010, _ecm_engine_data),
    (0x210, 0.020, _ecm_ac_data),
    (0x350, 0.020, _hvac_status),
    (0x400, 0.050, _bcm_body_status),
    (0x500, 0.020, _tcm_status),
]


class VirtualTransport(BaseTransport):
    """Mock transport for CI, testing, and offline development.

    Provides both UDS request/response simulation AND passive CAN broadcast
    emission via a python-can virtual bus channel.
    """

    def __init__(self, dtcs: list[str] | None = None, channel: str = "obd2_virtual") -> None:
        super().__init__()
        self._dtcs = dtcs if dtcs is not None else _SIMULATED_DTCS.copy()
        self._channel = channel
        self._bus: can.Bus | None = None
        self._emit_task: asyncio.Task | None = None
        self._start_time: float = 0.0

    async def connect(self) -> None:
        await asyncio.sleep(0.02)
        self._bus = can.Bus(
            interface="virtual",
            channel=self._channel,
            receive_own_messages=False,
        )
        self._start_time = time.monotonic()
        self._status = TransportStatus.CONNECTED
        self._device_info = {
            "type": "virtual",
            "channel": self._channel,
            "simulated_vehicle": "2020 Generic Truck 5.0L V8",
            "vin": "1GTFW1ET5EFA00001",
        }
        self._emit_task = asyncio.create_task(self._emit_loop(), name="virtual_can_emit")

    async def disconnect(self) -> None:
        if self._emit_task:
            self._emit_task.cancel()
            try:
                await self._emit_task
            except asyncio.CancelledError:
                pass
            self._emit_task = None
        if self._bus is not None:
            self._bus.shutdown()
            self._bus = None
        self._status = TransportStatus.DISCONNECTED

    def get_raw_bus(self) -> can.Bus | None:
        """Return the virtual can.Bus so CanSnifferProtocol can attach a Notifier."""
        return self._bus

    async def send_raw(self, data: bytes, tx_id: int, rx_id: int, timeout: float = 5.0) -> bytes:
        """Respond to OBD-II and UDS requests with canned data."""
        await asyncio.sleep(0.01)

        if len(data) < 1:
            return bytes([0x7F, 0x00, 0x10])

        mode = data[0]
        pid = data[1] if len(data) > 1 else 0x00

        if mode == 0x01 and pid in _SIMULATED_PIDS:
            return self._dynamic_pid(pid)

        if mode == 0x03:
            response = bytearray([0x43, len(self._dtcs)])
            for code in self._dtcs:
                code_int = int(code[1:], 16)
                prefix = {"P": 0x00, "C": 0x40, "B": 0x80, "U": 0xC0}[code[0]]
                response += struct.pack(">H", prefix | code_int)
            return bytes(response)

        if mode == 0x02:
            # Freeze Frame: [0x02, pid, frame_number]
            ff_pid = pid
            response = _FREEZE_FRAME_PIDS.get(ff_pid)
            if response:
                return response
            # Fall back to live-style static response with mode byte changed
            live = _SIMULATED_PIDS.get(ff_pid)
            if live:
                frame_byte = data[2] if len(data) > 2 else 0
                return bytes([0x42, ff_pid, frame_byte]) + live[2:]
            return bytes([0x7F, 0x02, 0x31])

        if mode == 0x04:
            self._dtcs.clear()
            return bytes([0x44])

        if mode == 0x06:
            mid = pid  # data[1]
            # Even multiples of 0x20 (0x00, 0x20, 0x40, …) are bitmask group queries
            if mid % 0x20 == 0:
                return _MODE06_MID_BITMASKS.get(
                    mid, bytes([0x46, mid, 0x00, 0x00, 0x00, 0x00])
                )
            # Any other MID: return per-MID test records
            return _MODE06_MID_RESPONSES.get(mid, bytes([0x7F, 0x06, 0x31]))

        if mode == 0x07:
            # Pending DTCs: one pending code P0171
            return bytes([0x47, 0x01, 0x01, 0x71])  # P0171

        if mode == 0x08:
            # Actuator test: echo back test_id with success
            test_id = pid
            return bytes([0x48, test_id, 0x01])

        if mode == 0x09:
            info_type = pid
            if info_type == 0x02:  # VIN
                return bytes([0x49, 0x02, 0x01]) + _VIN_ASCII
            if info_type == 0x04:  # Calibration ID
                return bytes([0x49, 0x04, 0x01]) + _CAL_ID
            if info_type == 0x06:  # CVN
                return bytes([0x49, 0x06, 0x01]) + _CVN_BYTES
            if info_type == 0x0A:  # ECU name
                return bytes([0x49, 0x0A, 0x01]) + _ECU_NAME
            return bytes([0x7F, 0x09, 0x31])

        if mode == 0x22 and data[1:3] == bytes([0xF1, 0x90]):
            vin = b"1GTFW1ET5EFA00001"
            return bytes([0x62, 0xF1, 0x90]) + vin

        if mode == 0x19:
            return bytes([0x59, data[1] if len(data) > 1 else 0x02,
                          0xFF, 0x00, len(self._dtcs)])

        # KWP2000 service 0x10 — StartDiagnosticSession
        if mode == 0x10:
            session = data[1] if len(data) > 1 else 0x89
            return bytes([0x50, session, 0x00, 0x19, 0x01])

        # KWP2000 service 0x13 — ReadDiagnosticTroubleCodes
        if mode == 0x13:
            response = bytearray([0x53])
            for code in self._dtcs:
                code_int = int(code[1:], 16)
                prefix = {"P": 0x00, "C": 0x40, "B": 0x80, "U": 0xC0}[code[0]]
                import struct as _s
                response += _s.pack(">H", prefix | code_int) + bytes([0x18])  # status: active + confirmed
            return bytes(response)

        # KWP2000 service 0x14 — ClearDiagnosticInformation
        if mode == 0x14:
            self._dtcs.clear()
            return bytes([0x54])

        # KWP2000 service 0x21 — ReadDataByLocalIdentifier (Honda)
        if mode == 0x21:
            lid = data[1] if len(data) > 1 else 0x11
            if lid == 0x11:
                # Simulated Honda LID 0x11: RPM=1000, speed=50, TPS=24%, MAP=101kPa, IAT=20°C, ECT=90°C, 14.4V
                payload = bytes([0x0F, 0xA0, 0x32, 0x61, 0xCB, 0x3C, 0x82, 0x90, 0x00, 0x00, 0x18])
                return bytes([0x61, lid]) + payload
            return bytes([0x61, lid]) + bytes(8)

        # KWP2000 service 0x3E — TesterPresent
        if mode == 0x3E:
            return bytes([0x7E])

        return bytes([0x7F, mode, 0x31])

    def _dynamic_pid(self, pid: int) -> bytes:
        """Return a live (time-varying) response for key Mode 01 PIDs."""
        t = time.monotonic() - self._start_time

        if pid == 0x0C:  # RPM: oscillates 800–1200
            rpm_raw = int((800 + 400 * math.sin(t * 0.5)) * 4) & 0xFFFF
            return bytes([0x41, 0x0C, rpm_raw >> 8, rpm_raw & 0xFF])

        if pid == 0x0D:  # Speed: oscillates 45–55 km/h
            speed = int(50 + 5 * math.sin(t * 0.3)) & 0xFF
            return bytes([0x41, 0x0D, speed])

        if pid == 0x04:  # Engine load: oscillates 15–25%
            load_raw = int((20 + 5 * math.sin(t * 0.4)) * 255 / 100) & 0xFF
            return bytes([0x41, 0x04, load_raw])

        if pid == 0x11:  # Throttle: oscillates 14–24%
            tps_raw = int((19 + 5 * math.sin(t * 0.3)) * 255 / 100) & 0xFF
            return bytes([0x41, 0x11, tps_raw])

        if pid == 0x05:  # Coolant: slowly rises 70→90°C over 2 min
            temp = min(90, 70 + int(t / 6)) + 40  # +40 offset in raw byte
            return bytes([0x41, 0x05, temp & 0xFF])

        if pid == 0x10:  # MAF: oscillates 9–13 g/s
            maf_raw = int((1100 + 200 * math.sin(t * 0.5))) & 0xFFFF
            return bytes([0x41, 0x10, maf_raw >> 8, maf_raw & 0xFF])

        if pid == 0x1F:  # Engine run time: counts up in seconds
            elapsed = int(t) & 0xFFFF
            return bytes([0x41, 0x1F, elapsed >> 8, elapsed & 0xFF])

        return _SIMULATED_PIDS[pid]

    async def list_devices(self) -> list[dict[str, Any]]:
        return [{
            "type": "virtual",
            "channel": self._channel,
            "description": "Simulated OBD-II vehicle (no hardware required)",
        }]

    # ------------------------------------------------------------------
    # Background CAN broadcast emitter
    # ------------------------------------------------------------------

    async def _emit_loop(self) -> None:
        """Emit simulated CAN broadcast frames on a realistic schedule."""
        next_send = {arb_id: time.monotonic() for arb_id, _, _ in _BROADCAST_SCHEDULE}

        while True:
            now = time.monotonic()
            t = now - self._start_time
            earliest_next = now + 1.0

            for arb_id, period, builder in _BROADCAST_SCHEDULE:
                if now >= next_send[arb_id]:
                    try:
                        data = builder(t)
                        msg = can.Message(
                            arbitration_id=arb_id,
                            data=data,
                            is_extended_id=False,
                            timestamp=now,
                        )
                        if self._bus is not None:
                            self._bus.send(msg)
                    except Exception:
                        pass
                    next_send[arb_id] = now + period

                earliest_next = min(earliest_next, next_send[arb_id])

            sleep_time = max(0.001, earliest_next - time.monotonic())
            await asyncio.sleep(sleep_time)

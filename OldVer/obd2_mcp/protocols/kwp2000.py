"""KWP2000 (ISO 14230 / SAE J2190) protocol layer.

KWP2000 is the K-Line diagnostic protocol used on:
  - Honda / Acura (approx. 2000–2008)
  - Toyota / Lexus pre-CAN
  - Many European vehicles pre-2008 OBD-II CAN mandate

Service IDs differ from UDS ISO-14229 but the diagnostic model is similar.
Where both protocols share a service (e.g. 0x22 ReadDataByCommonIdentifier,
0x14 ClearDiagnosticInformation) the byte encodings are compatible.

Honda-specific notes:
  - Live data is accessed via service 0x21 (ReadDataByLocalIdentifier) with
    Honda-proprietary Local Identifiers (LIDs), not Mode 01 PIDs.
  - DTC format is 3 bytes per record: [high, low, status].
  - Some Honda ECUs also respond to standard OBD-II Mode 01/03/04 requests
    over K-Line; use OBD2Protocol for those.
"""

from __future__ import annotations

import asyncio
from typing import Any

from obd2_mcp.transport.base import BaseTransport

# ── Service IDs ───────────────────────────────────────────────────────────────
_SVC_START_SESSION       = 0x10
_SVC_ECU_RESET           = 0x11
_SVC_READ_FREEZE_FRAME   = 0x12
_SVC_READ_DTCS           = 0x13
_SVC_CLEAR_DTCS          = 0x14
_SVC_READ_STATUS_OF_DTC  = 0x17
_SVC_READ_BY_LOCAL_ID    = 0x21
_SVC_READ_BY_COMMON_ID   = 0x22
_SVC_TESTER_PRESENT      = 0x3E

# ── Session modes ─────────────────────────────────────────────────────────────
SESSION_STANDARD    = 0x89   # standardDiagnosticSession
SESSION_EXTENDED    = 0x85   # extendedDiagnosticSession
SESSION_HONDA_DIAG  = 0x82   # Honda dealer diagnostic session

# ── Honda Local Identifiers (LID) ─────────────────────────────────────────────
# These are Honda-proprietary; values vary slightly by model year / ECU type.
# The layouts below match typical 5th–8th gen Civic / Accord K-series ECUs.
HONDA_LID_ENGINE    = 0x11   # RPM, speed, TPS, MAP, IAT, ECT, battery
HONDA_LID_O2        = 0x12   # Front / rear O2 sensor voltages
HONDA_LID_FUEL_TRIM = 0x14   # Short-term and long-term fuel trim

# ── DTC helpers ───────────────────────────────────────────────────────────────
_DTC_PREFIX = {0: "P", 1: "C", 2: "B", 3: "U"}

_DTC_STATUS_BITS = {
    0: "test not completed since clear",
    1: "test failed since clear",
    2: "test completed this drive cycle",
    3: "fault currently active",
    4: "confirmed DTC",
    5: "test not completed this cycle",
    6: "warning indicator requested",
    7: "test failed since last clear",
}


def _parse_kwp_dtcs(response: bytes | None) -> list[dict[str, Any]]:
    """Parse a KWP2000 service 0x13 response into a list of DTC dicts."""
    if not response or response[0] != 0x53:
        return []

    dtcs: list[dict[str, Any]] = []
    payload = response[1:]  # skip positive response SID 0x53

    # 3-byte records: [high_byte, low_byte, status_byte]
    for i in range(0, len(payload) - 2, 3):
        high, low, status = payload[i], payload[i + 1], payload[i + 2]
        word = (high << 8) | low
        if word == 0:
            continue
        prefix   = _DTC_PREFIX[(word >> 14) & 0x03]
        code_num = word & 0x3FFF
        dtcs.append({
            "code":       f"{prefix}{code_num:04X}",
            "status_byte": hex(status),
            "active":     bool(status & 0x08),
            "confirmed":  bool(status & 0x10),
            "flags":      [_DTC_STATUS_BITS[b] for b in range(8) if status & (1 << b)],
        })
    return dtcs


def _parse_honda_lid_engine(data: bytes) -> dict[str, Any]:
    """Parse Honda K-Line LID 0x11 primary engine data block.

    Layout (K-series ECU, 11+ bytes):
      [0–1] Engine RPM  — raw / 4 → rpm
      [2]   Vehicle speed — km/h
      [3]   Throttle position — % = byte / 2.55
      [4]   MAP — kPa ≈ byte × 0.5
      [5]   Intake air temp — °C = byte − 40
      [6]   Coolant temp — °C = byte − 40
      [7]   Battery voltage — V = byte × 0.1
      [8–9] Injector pulse width — ms = word × 0.256
      [10]  Ignition advance — deg = (byte − 24) × 1.0
    """
    if len(data) < 8:
        return {"raw": data.hex(), "note": "response too short to decode"}

    result: dict[str, Any] = {
        "local_id": "0x11",
        "engine_rpm":         ((data[0] << 8) | data[1]) * 4,
        "vehicle_speed_kmh":  data[2],
        "throttle_pct":       round(data[3] / 2.55, 1),
        "map_kpa":            round(data[4] * 0.5, 1),
        "intake_air_temp_c":  data[5] - 40,
        "coolant_temp_c":     data[6] - 40,
        "battery_v":          round(data[7] * 0.1, 1),
    }
    if len(data) > 9:
        result["injector_pw_ms"] = round(((data[8] << 8) | data[9]) * 0.256, 2)
    if len(data) > 10:
        result["ignition_advance_deg"] = round((data[10] - 24) * 1.0, 1)
    result["raw"] = data.hex()
    return result


def _parse_honda_lid_o2(data: bytes) -> dict[str, Any]:
    """Parse Honda K-Line LID 0x12 O2 sensor block."""
    if len(data) < 2:
        return {"raw": data.hex()}
    return {
        "local_id":         "0x12",
        "front_o2_v":       round(data[0] * 0.00488, 3),   # 5V / 1024 steps
        "rear_o2_v":        round(data[1] * 0.00488, 3) if len(data) > 1 else None,
        "raw":              data.hex(),
    }


def _parse_honda_lid_fuel_trim(data: bytes) -> dict[str, Any]:
    """Parse Honda K-Line LID 0x14 fuel trim block."""
    if len(data) < 2:
        return {"raw": data.hex()}
    # Honda encodes STFT/LTFT as signed values centred at 128 (0% = 128)
    stft_pct = round((data[0] - 128) / 128 * 100, 1)
    ltft_pct = round((data[1] - 128) / 128 * 100, 1) if len(data) > 1 else None
    return {
        "local_id":   "0x14",
        "stft_pct":   stft_pct,
        "ltft_pct":   ltft_pct,
        "raw":        data.hex(),
    }


_LID_PARSERS = {
    HONDA_LID_ENGINE:    _parse_honda_lid_engine,
    HONDA_LID_O2:        _parse_honda_lid_o2,
    HONDA_LID_FUEL_TRIM: _parse_honda_lid_fuel_trim,
}


# ── Protocol class ────────────────────────────────────────────────────────────

class KWP2000Protocol:
    """High-level KWP2000 diagnostic services for K-Line ECUs.

    All public methods are async coroutines; they delegate framing to the
    underlying transport (typically KLineTransport).

    Usage::

        transport = KLineTransport()
        await transport.connect()
        kwp = KWP2000Protocol(transport)
        dtcs = await kwp.read_dtcs()
        data = await kwp.read_data_by_local_id(HONDA_LID_ENGINE)
    """

    # Default K-Line addressing used for all requests unless overridden
    _TX_ID = 0x33   # OBD-II K-Line functional broadcast
    _RX_ID = 0xF1   # Standard external tester address

    def __init__(self, transport: BaseTransport) -> None:
        self._transport = transport

    # ── session management ────────────────────────────────────────────────────

    async def start_session(self, mode: int = SESSION_STANDARD) -> dict[str, Any]:
        """Start or change a KWP2000 diagnostic session (service 0x10)."""
        resp = await self._transport.send_raw(
            bytes([_SVC_START_SESSION, mode]), self._TX_ID, self._RX_ID
        )
        if resp and resp[0] == 0x50:
            p2_max = (resp[2] * 25) if len(resp) > 2 else None
            return {"success": True, "session": hex(mode), "p2_max_ms": p2_max}
        return self._negative_response(resp)

    async def tester_present(self) -> bool:
        """Send TesterPresent keep-alive (service 0x3E)."""
        resp = await self._transport.send_raw(
            bytes([_SVC_TESTER_PRESENT, 0x01]), self._TX_ID, self._RX_ID
        )
        return bool(resp and resp[0] == 0x7E)

    # ── DTCs ──────────────────────────────────────────────────────────────────

    async def read_dtcs(self, status_group: int = 0xFF) -> list[dict[str, Any]]:
        """Read stored DTCs via service 0x13.

        Args:
            status_group: DTC status group byte (0xFF = all).

        Returns:
            List of DTC dicts: {code, status_byte, active, confirmed, flags}.
        """
        resp = await self._transport.send_raw(
            bytes([_SVC_READ_DTCS, status_group]), self._TX_ID, self._RX_ID
        )
        return _parse_kwp_dtcs(resp)

    async def clear_dtcs(self, group_high: int = 0xFF, group_low: int = 0x00) -> bool:
        """Clear DTCs via service 0x14.  Default: all DTCs (group 0xFF00)."""
        resp = await self._transport.send_raw(
            bytes([_SVC_CLEAR_DTCS, group_high, group_low]),
            self._TX_ID, self._RX_ID,
        )
        return bool(resp and resp[0] == 0x54)

    async def read_status_of_dtc(self, dtc_high: int, dtc_low: int) -> dict[str, Any]:
        """Query the status of a specific DTC (service 0x17)."""
        resp = await self._transport.send_raw(
            bytes([_SVC_READ_STATUS_OF_DTC, dtc_high, dtc_low, 0xFF]),
            self._TX_ID, self._RX_ID,
        )
        if not resp or resp[0] != 0x57:
            return self._negative_response(resp)
        return {
            "dtc":        f"{_DTC_PREFIX[(((dtc_high << 8) | dtc_low) >> 14) & 3]}"
                          f"{((dtc_high << 8) | dtc_low) & 0x3FFF:04X}",
            "status":     hex(resp[3]) if len(resp) > 3 else "unknown",
            "raw":        resp.hex(),
        }

    # ── live data ─────────────────────────────────────────────────────────────

    async def read_data_by_local_id(self, local_id: int) -> dict[str, Any]:
        """Read a data block by Honda Local Identifier (service 0x21).

        Known LIDs with structured parsing:
          0x11 — engine block (RPM, speed, TPS, MAP, temps, battery, IPW)
          0x12 — O2 sensors
          0x14 — fuel trim (STFT / LTFT)

        All other LIDs return raw hex bytes.
        """
        resp = await self._transport.send_raw(
            bytes([_SVC_READ_BY_LOCAL_ID, local_id]), self._TX_ID, self._RX_ID
        )
        if not resp or resp[0] != 0x61:
            return self._negative_response(resp)

        payload = resp[2:]  # skip response SID 0x61 + echoed local_id
        parser  = _LID_PARSERS.get(local_id)
        if parser:
            return parser(payload)
        return {"local_id": hex(local_id), "raw": payload.hex(), "length": len(payload)}

    async def read_data_by_common_id(self, did: int) -> dict[str, Any]:
        """Read a Data Identifier (service 0x22 — same as UDS 0x22)."""
        resp = await self._transport.send_raw(
            bytes([_SVC_READ_BY_COMMON_ID, (did >> 8) & 0xFF, did & 0xFF]),
            self._TX_ID, self._RX_ID,
        )
        if not resp or resp[0] != 0x62:
            return self._negative_response(resp)
        payload = resp[3:]  # skip 0x62 + did_high + did_low
        try:
            text = payload.decode("ascii", errors="replace").strip("\x00").strip()
        except Exception:
            text = None
        return {"did": hex(did), "value": text or payload.hex(), "raw": payload.hex()}

    async def read_freeze_frame(self, dtc_high: int, dtc_low: int) -> dict[str, Any]:
        """Read freeze-frame data for a specific DTC (service 0x12)."""
        resp = await self._transport.send_raw(
            bytes([_SVC_READ_FREEZE_FRAME, dtc_high, dtc_low, 0x00]),
            self._TX_ID, self._RX_ID,
        )
        if not resp or resp[0] != 0x52:
            return self._negative_response(resp)
        return {"dtc": f"{dtc_high:02X}{dtc_low:02X}", "raw": resp[3:].hex()}

    # ── control ───────────────────────────────────────────────────────────────

    async def ecu_reset(self, reset_type: int = 0x01) -> dict[str, Any]:
        """Reset the ECU (service 0x11).  reset_type: 1=hard, 3=soft."""
        resp = await self._transport.send_raw(
            bytes([_SVC_ECU_RESET, reset_type]), self._TX_ID, self._RX_ID
        )
        return {"success": bool(resp and resp[0] == 0x51), "raw": resp.hex() if resp else None}

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _negative_response(resp: bytes | None) -> dict[str, Any]:
        if resp and resp[0] == 0x7F:
            nrc = resp[2] if len(resp) > 2 else 0xFF
            return {"success": False, "nrc": hex(nrc), "raw": resp.hex()}
        return {"success": False, "raw": resp.hex() if resp else "empty"}

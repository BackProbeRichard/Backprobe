"""OBD-II protocol layer (Modes 01–0A).

When transport is ELM327, delegates to python-obd.
When transport is J2534 or SocketCAN, uses raw send_raw() calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

from obd2_mcp.transport.base import BaseTransport


class OBD2Protocol:
    """High-level OBD-II service methods."""

    def __init__(self, transport: BaseTransport) -> None:
        self._transport = transport

    # ------------------------------------------------------------------
    # Mode 01 -Current data (live PIDs)
    # ------------------------------------------------------------------

    async def query_pid(
        self, pid: int, mode: int = 0x01, timeout: float | None = None
    ) -> dict[str, Any]:
        """Query a single OBD-II PID.

        If transport has a python-obd connection, uses that (rich unit parsing).
        Otherwise falls back to raw bytes.

        ``timeout`` overrides the per-request wait (seconds) on the raw path;
        live-data polling passes a short value so unanswered PIDs fail fast.
        """
        obd_conn = self._transport.get_obd_connection()
        if obd_conn is not None:
            return await self._query_via_obd(obd_conn, pid, mode)
        return await self._query_raw(pid, mode, timeout=timeout)

    async def _query_via_obd(self, conn: Any, pid: int, mode: int) -> dict[str, Any]:
        import obd

        loop = asyncio.get_event_loop()
        # Find command by pid
        cmd = self._find_obd_command(pid, mode)
        if cmd is None:
            return {"pid": hex(pid), "mode": hex(mode), "error": "unknown PID"}

        response = await loop.run_in_executor(None, conn.query, cmd)
        return {
            "pid": hex(pid),
            "mode": hex(mode),
            "name": cmd.name,
            "value": str(response.value) if response.value is not None else None,
            "unit": str(response.unit) if hasattr(response, "unit") else None,
            "raw": response.raw_response,
        }

    def _find_obd_command(self, pid: int, mode: int) -> Any:
        import obd

        for cmd in obd.commands[mode]:
            if hasattr(cmd, "pid") and cmd.pid == pid:
                return cmd
        return None

    async def _query_raw(
        self, pid: int, mode: int, timeout: float | None = None
    ) -> dict[str, Any]:
        from obd2_mcp.config import settings

        data = bytes([mode, pid])
        try:
            response = await self._transport.send_raw(
                data,
                tx_id=settings.uds_tx_id,
                rx_id=settings.uds_rx_id,
                timeout=timeout if timeout is not None else settings.obd_poll_timeout,
            )
            result: dict[str, Any] = {
                "pid": hex(pid),
                "mode": hex(mode),
                "raw_hex": response.hex(),
            }
            parsed = _parse_pid_value(pid, response)
            if parsed:
                result.update(parsed)
            return result
        except Exception as exc:
            return {"pid": hex(pid), "mode": hex(mode), "error": str(exc)}

    # ------------------------------------------------------------------
    # Mode 03 -Read stored DTCs
    # ------------------------------------------------------------------

    async def read_dtcs(self) -> list[str]:
        """Return a list of stored DTC strings (e.g. ['P0300', 'P0420'])."""
        obd_conn = self._transport.get_obd_connection()
        if obd_conn is not None:
            return await self._read_dtcs_via_obd(obd_conn)
        return await self._read_dtcs_raw()

    async def _read_dtcs_via_obd(self, conn: Any) -> list[str]:
        import obd

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, conn.query, obd.commands.GET_DTC)
        if response.value is None:
            return []
        return [str(code) for code, _ in response.value]

    async def _read_dtcs_raw(self) -> list[str]:
        from obd2_mcp.config import settings

        data = bytes([0x03])
        try:
            response = await self._transport.send_raw(
                data,
                tx_id=settings.uds_tx_id,
                rx_id=settings.uds_rx_id,
            )
            return _parse_mode03_response(response)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Mode 01 -PID discovery via support bitmask
    # ------------------------------------------------------------------

    async def discover_supported_pids(self) -> list[int]:
        """Walk Mode 01 bitmask PIDs (0x00, 0x20, 0x40 …) and return all supported PIDs.

        Each group's 4-byte bitmask encodes PIDs base+1 through base+32.
        Bit 0 (LSB) means the next group (base+32) is also supported.
        """
        supported: list[int] = []
        base = 0x00
        while base <= 0xE0:
            result = await self._query_raw(base, 0x01)
            if "error" in result:
                break
            try:
                raw = bytes.fromhex(result.get("raw_hex", ""))
            except ValueError:
                break
            if len(raw) < 6:
                break
            bitmask = int.from_bytes(raw[2:6], "big")
            for k in range(1, 32):  # PIDs base+1 … base+31 (base+32 is the group indicator)
                if bitmask & (1 << (32 - k)):
                    supported.append(base + k)
            if not (bitmask & 0x01):
                break
            base += 0x20
        return supported

    # ------------------------------------------------------------------
    # Mode 02 -Freeze Frame
    # ------------------------------------------------------------------

    async def query_freeze_frame(self, pid: int, frame: int = 0) -> dict[str, Any]:
        """Query a single Mode 02 freeze frame PID."""
        from obd2_mcp.config import settings

        data = bytes([0x02, pid, frame])
        try:
            response = await self._transport.send_raw(
                data, tx_id=settings.uds_tx_id, rx_id=settings.uds_rx_id,
                timeout=settings.obd_read_timeout,
            )
            result: dict[str, Any] = {"pid": hex(pid), "frame": frame, "raw_hex": response.hex()}
            if len(response) >= 3 and response[0] == 0x42 and response[1] == pid:
                fake_m01 = bytes([0x41, pid]) + response[3:]
                parsed = _parse_pid_value(pid, fake_m01)
                if parsed:
                    result.update(parsed)
            return result
        except Exception as exc:
            return {"pid": hex(pid), "frame": frame, "error": str(exc)}

    async def read_freeze_frame(
        self, pids: list[int] | None = None, frame: int = 0
    ) -> list[dict[str, Any]]:
        """Read multiple freeze frame PIDs concurrently."""
        target = pids if pids is not None else [
            0x0C, 0x0D, 0x05, 0x04, 0x11, 0x0B, 0x0F, 0x10, 0x0E, 0x1F, 0x2F,
        ]
        tasks = [self.query_freeze_frame(pid, frame) for pid in target]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r if isinstance(r, dict) else {"error": str(r)} for r in results]

    # ------------------------------------------------------------------
    # Mode 06 -On-board monitor test results
    # ------------------------------------------------------------------

    async def discover_monitor_mids(self) -> list[int]:
        """Walk Mode 06 MID support bitmasks and return all supported MIDs.

        Mode 06 has the same bitmask-walk structure as Mode 01 PID discovery.
        MID 0x00 returns a 4-byte bitmask for MIDs 0x01–0x1F; 0x20 for 0x21–0x3F, etc.
        The bitmask LSB (bit 0) indicates whether the next group (MID base+32) exists.
        """
        from obd2_mcp.config import settings

        supported: list[int] = []
        base = 0x00
        while base <= 0xE0:
            data = bytes([0x06, base])
            try:
                response = await self._transport.send_raw(
                    data, tx_id=settings.uds_tx_id, rx_id=settings.uds_rx_id,
                    timeout=settings.obd_read_timeout,
                )
            except Exception:
                break
            if not response or len(response) < 6 or response[0] != 0x46 or response[1] != base:
                break
            bitmask = int.from_bytes(response[2:6], "big")
            for k in range(1, 32):
                if bitmask & (1 << (32 - k)):
                    supported.append(base + k)
            if not (bitmask & 0x01):
                break
            base += 0x20
        return supported

    async def read_monitor_tests(self) -> list[dict[str, Any]]:
        """Discover supported Mode 06 MIDs, then query each one for test results.

        Each MID response contains one or more TID records with actual/min/max values
        and a scaling ID that maps raw integers to engineering units.
        """
        from obd2_mcp.config import settings

        mids = await self.discover_monitor_mids()
        all_records: list[dict[str, Any]] = []
        for mid in mids:
            data = bytes([0x06, mid])
            try:
                response = await self._transport.send_raw(
                    data, tx_id=settings.uds_tx_id, rx_id=settings.uds_rx_id,
                    timeout=settings.obd_read_timeout,
                )
                all_records.extend(_parse_mode06_mid_response(mid, response))
            except Exception:
                continue
        return all_records

    # ------------------------------------------------------------------
    # Mode 07 -Pending DTCs
    # ------------------------------------------------------------------

    async def read_pending_dtcs(self) -> list[str]:
        """Read Mode 07 pending (not yet confirmed) DTCs."""
        from obd2_mcp.config import settings

        data = bytes([0x07])
        try:
            response = await self._transport.send_raw(
                data, tx_id=settings.uds_tx_id, rx_id=settings.uds_rx_id,
            )
            return _parse_mode07_response(response)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Mode 08 -Actuator tests
    # ------------------------------------------------------------------

    async def run_actuator_test(self, test_id: int, enable: bool = True) -> dict[str, Any]:
        """Send a Mode 08 actuator test command."""
        from obd2_mcp.config import settings

        data = bytes([0x08, test_id, 0x01 if enable else 0x00])
        try:
            response = await self._transport.send_raw(
                data, tx_id=settings.uds_tx_id, rx_id=settings.uds_rx_id,
            )
            ok = len(response) >= 2 and response[0] == 0x48 and response[1] == test_id
            return {
                "test_id": hex(test_id),
                "status": "ok" if ok else "failed",
                "raw_hex": response.hex(),
            }
        except Exception as exc:
            return {"test_id": hex(test_id), "error": str(exc)}

    # ------------------------------------------------------------------
    # Mode 09 -Vehicle information
    # ------------------------------------------------------------------

    async def read_vin(self) -> str | None:
        """Read just the VIN (Mode 09 PID 02) as a fast vehicle-identity probe.

        Returns the VIN string, or None if the ECU didn't answer / data was bad.
        Used on connect once PID discovery has confirmed the vehicle is awake.
        """
        from obd2_mcp.config import settings

        data = bytes([0x09, 0x02])
        try:
            response = await self._transport.send_raw(
                data, tx_id=settings.uds_tx_id, rx_id=settings.uds_rx_id,
                timeout=settings.obd_read_timeout,
            )
        except Exception:
            return None
        vin = _parse_mode09_response(0x02, response)
        return vin if vin and vin != "—" else None

    async def read_vehicle_info(self) -> dict[str, Any]:
        """Read Mode 09 vehicle information: VIN, Cal ID, CVN, ECU name."""
        from obd2_mcp.config import settings

        info: dict[str, Any] = {}
        for info_type, key in [
            (0x02, "vin"),
            (0x04, "cal_id"),
            (0x06, "cvn"),
            (0x0A, "ecu_name"),
        ]:
            data = bytes([0x09, info_type])
            try:
                response = await self._transport.send_raw(
                    data, tx_id=settings.uds_tx_id, rx_id=settings.uds_rx_id,
                    timeout=settings.obd_read_timeout,
                )
                info[key] = _parse_mode09_response(info_type, response)
            except Exception as exc:
                info[key] = f"Error: {exc}"
        return info

    # ------------------------------------------------------------------
    # Mode 04 -Clear DTCs
    # ------------------------------------------------------------------

    async def clear_dtcs(self) -> bool:
        """Clear all stored DTCs. Returns True on success."""
        obd_conn = self._transport.get_obd_connection()
        if obd_conn is not None:
            import obd

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, obd_conn.query, obd.commands.CLEAR_DTC)
            return not response.is_null()

        from obd2_mcp.config import settings

        data = bytes([0x04])
        try:
            response = await self._transport.send_raw(
                data,
                tx_id=settings.uds_tx_id,
                rx_id=settings.uds_rx_id,
            )
            return len(response) > 0 and response[0] == 0x44
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Multi-PID snapshot
    # ------------------------------------------------------------------

    async def live_data_snapshot(self, pids: list[int] | None = None) -> list[dict[str, Any]]:
        """Query multiple PIDs concurrently and return a snapshot."""
        default_pids = [0x0C, 0x0D, 0x05, 0x04, 0x0B, 0x11, 0x0F, 0x1F]
        target_pids = pids if pids is not None else default_pids
        tasks = [self.query_pid(pid) for pid in target_pids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r if isinstance(r, dict) else {"error": str(r)} for r in results]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FUEL_TYPES = {
    0: "Not available", 1: "Gasoline", 2: "Methanol", 3: "Ethanol",
    4: "Diesel", 5: "LPG", 6: "CNG", 7: "Propane", 8: "Electric",
    9: "Bifuel (gasoline+electric)", 10: "Bifuel (gasoline+CNG)",
}


def _parse_pid_value(pid: int, response: bytes) -> dict[str, Any] | None:
    """Parse Mode 01 raw response bytes into human-readable value + unit.

    Response format: [mode+0x40, pid, A, B, C, D, ...]
    Returns None if the PID is unknown or the response is malformed.
    """
    if len(response) < 2 or response[0] != 0x41 or response[1] != pid:
        return None
    data = response[2:]  # payload bytes A, B, C…
    A = data[0] if len(data) > 0 else 0
    B = data[1] if len(data) > 1 else 0
    C = data[2] if len(data) > 2 else 0
    D = data[3] if len(data) > 3 else 0

    if pid == 0x04:
        return {"value": round(A * 100.0 / 255.0, 1), "unit": "%"}
    if pid == 0x05:
        return {"value": A - 40, "unit": "°C"}
    if pid == 0x06:
        return {"value": round((A - 128) * 100.0 / 128.0, 1), "unit": "%"}
    if pid == 0x07:
        return {"value": round((A - 128) * 100.0 / 128.0, 1), "unit": "%"}
    if pid == 0x0B:
        return {"value": A, "unit": "kPa"}
    if pid == 0x0C:
        return {"value": round((A * 256 + B) / 4.0, 0), "unit": "rpm"}
    if pid == 0x0D:
        return {"value": A, "unit": "km/h"}
    if pid == 0x0E:
        return {"value": round(A / 2.0 - 64.0, 1), "unit": "° before TDC"}
    if pid == 0x0F:
        return {"value": A - 40, "unit": "°C"}
    if pid == 0x10:
        return {"value": round((A * 256 + B) / 100.0, 2), "unit": "g/s"}
    if pid == 0x11:
        return {"value": round(A * 100.0 / 255.0, 1), "unit": "%"}
    if pid == 0x1F:
        return {"value": A * 256 + B, "unit": "s"}
    if pid == 0x2F:
        return {"value": round(A * 100.0 / 255.0, 1), "unit": "%"}
    if pid == 0x33:
        return {"value": A, "unit": "kPa"}
    if pid == 0x46:
        return {"value": A - 40, "unit": "°C"}
    if pid == 0x5C:
        return {"value": A - 40, "unit": "°C"}
    if pid == 0x51:
        return {"value": _FUEL_TYPES.get(A, f"Type {A}"), "unit": ""}
    return None


# SAE J1979 / ISO 15031-5 Table A.2 -Unit and Scaling IDs
# Maps scaling_id → (multiplier, unit_string)
_MODE06_SCALING: dict[int, tuple[float, str]] = {
    0x01: (0.001,         "V"),       # 1 mV/bit
    0x09: (100.0 / 256,  "%"),        # 0.390625 %/bit
    0x0A: (0.005,         "V"),       # 5 mV/bit  (O2 sensor voltage)
    0x0B: (0.1,           "mA"),      # 0.1 mA/bit
    0x0C: (1.0,           "ohm"),     # 1 ohm/bit
    0x0D: (0.01,          "kPa"),     # 0.01 kPa/bit
    0x0E: (0.005,         "kPa"),     # 0.005 kPa/bit
    0x0F: (0.1,           "kPa"),     # 0.1 kPa/bit
    0x10: (1.0,           "ms"),      # 1 ms/bit
    0x11: (0.01,          "rpm"),     # 0.01 rpm/bit
    0x13: (1.0,           "counts"),  # raw count (misfire counts, etc.)
    0x14: (1.0,           "counts"),
    0x16: (1.0,           "km"),
    0x18: (0.5,           "°C"),      # 0.5 °C/bit
    0x1A: (0.1,           "s"),       # 0.1 s/bit
    0x22: (1.0,           "s"),       # 1 s/bit
    0x81: (0.01,          "%"),       # 0.01 %/bit  (catalyst efficiency, etc.)
    0x82: (0.001,         "g/s"),     # 0.001 g/s per bit
    0x83: (0.1,           "g/s"),     # 0.1 g/s per bit
}

# (mid, tid) → human description
# MIDs 0x01–0x0F: O2 sensor tests        MIDs 0x21–0x2F: catalyst
# MIDs 0x41–0x5F: EVAP                   MIDs 0x61–0x7F: EGR/VVT
# MIDs 0x81+:     OEM-defined (misfire, etc.)
_MONITOR_DESCRIPTIONS: dict[tuple[int, int], str] = {
    # O2 sensor monitor
    (0x01, 0x01): "O2 Sensor B1S1 -Min Voltage",
    (0x01, 0x02): "O2 Sensor B1S1 -Max Voltage",
    (0x01, 0x0B): "O2 Heater B1S1 -Resistance",
    (0x01, 0x0C): "O2 Heater B1S2 -Resistance",
    (0x02, 0x01): "O2 Sensor B1S2 -Min Voltage",
    (0x02, 0x02): "O2 Sensor B1S2 -Max Voltage",
    (0x03, 0x0B): "O2 Heater B2S1 -Resistance",
    (0x03, 0x0C): "O2 Heater B2S2 -Resistance",
    # Catalyst monitor
    (0x21, 0x01): "Catalyst B1 -Min Temp",
    (0x21, 0x02): "Catalyst B1 -Efficiency",
    (0x22, 0x01): "Catalyst B2 -Min Temp",
    (0x22, 0x02): "Catalyst B2 -Efficiency",
    # EVAP monitor
    (0x41, 0x04): "EVAP -Purge Flow",
    (0x41, 0x05): "EVAP -Gross Leak",
    (0x42, 0x05): "EVAP -Small Leak (0.040\")",
    # EGR/VVT
    (0x61, 0x00): "EGR -Flow",
    (0x61, 0x01): "EGR -Bypass",
    (0x62, 0x00): "VVT -Bank 1",
    (0x63, 0x00): "VVT -Bank 2",
}


def _apply_mode06_scaling(scaling_id: int, raw: int) -> tuple[str, str]:
    """Convert a raw Mode 06 test value to (value_string, unit_string).

    Uses the SAE J1979 Table A.2 scaling table.  Unknown scaling IDs are
    shown as raw integers with the scaling ID in hex so the data is not lost.
    """
    if scaling_id in _MODE06_SCALING:
        factor, unit = _MODE06_SCALING[scaling_id]
        val = raw * factor
        # Show enough decimal places to be meaningful, trim trailing zeros
        val_str = f"{val:.4f}".rstrip("0").rstrip(".")
        return val_str, unit
    return str(raw), f"(scale {scaling_id:#04x})"


def _parse_mode06_mid_response(mid: int, data: bytes) -> list[dict[str, Any]]:
    """Parse a Mode 06 response for a specific MID into TID test records.

    Wire format (per SAE J1979 §6.6.1): a 0x46 byte followed by repeating
    9-byte records, each of which carries its OWN MID:
      [0x46] [MID] [TID] [UASID] [VAL_H] [VAL_L] [MIN_H] [MIN_L] [MAX_H] [MAX_L]
             └──────────────────── 9-byte record ───────────────────────┘
             [MID] [TID] ...  (next record)

    The MID is repeated in every record (a single response may even span more
    than one MID). A pass/fail is determined by: min_raw ≤ actual_raw ≤ max_raw.
    """
    if not data or len(data) < 10 or data[0] != 0x46:
        return []
    records = []
    i = 1
    while i + 8 < len(data):
        rec_mid    = data[i]
        tid        = data[i + 1]
        scaling_id = data[i + 2]
        raw_val    = (data[i + 3] << 8) | data[i + 4]
        raw_min    = (data[i + 5] << 8) | data[i + 6]
        raw_max    = (data[i + 7] << 8) | data[i + 8]

        actual_str, unit = _apply_mode06_scaling(scaling_id, raw_val)
        min_str,    _    = _apply_mode06_scaling(scaling_id, raw_min)
        max_str,    _    = _apply_mode06_scaling(scaling_id, raw_max)

        desc = _MONITOR_DESCRIPTIONS.get(
            (rec_mid, tid),
            f"MID {rec_mid:#04x} TID {tid:#04x}",
        )

        records.append({
            "mid": rec_mid,
            "tid": tid,
            "description": desc,
            "actual": actual_str,
            "min": min_str,
            "max": max_str,
            "unit": unit,
            "passed": raw_min <= raw_val <= raw_max,
        })
        i += 9
    return records


def _parse_mode07_response(data: bytes) -> list[str]:
    """Parse Mode 07 pending DTC response into DTC strings."""
    import struct

    if not data or data[0] != 0x47:
        return []
    dtcs = []
    payload = data[2:]  # skip 0x47 and count byte
    for i in range(0, len(payload) - 1, 2):
        word = struct.unpack(">H", payload[i : i + 2])[0]
        if word == 0:
            continue
        prefix = {0: "P", 1: "C", 2: "B", 3: "U"}[(word >> 14) & 0x03]
        dtcs.append(f"{prefix}{word & 0x3FFF:04X}")
    return dtcs


def _parse_mode09_response(info_type: int, data: bytes) -> str:
    """Parse a Mode 09 response for a single info_type into a readable string."""
    if not data or len(data) < 3 or data[0] != 0x49 or data[1] != info_type:
        return "—"
    payload = data[3:]  # skip 0x49, info_type, count byte
    try:
        if info_type in (0x02, 0x04, 0x0A):  # ASCII fields
            return payload.decode("ascii", errors="replace").rstrip("\x00 ")
        if info_type == 0x06:  # CVN: hex bytes
            return payload.hex().upper()
    except Exception:
        pass
    return payload.hex()


def _parse_mode03_response(data: bytes) -> list[str]:
    """Parse a raw Mode 03 response into DTC strings."""
    import struct

    if not data or data[0] != 0x43:
        return []
    dtcs = []
    payload = data[2:]  # skip 0x43 and count byte
    for i in range(0, len(payload) - 1, 2):
        word = struct.unpack(">H", payload[i : i + 2])[0]
        if word == 0:
            continue
        prefix_bits = (word >> 14) & 0x03
        prefix = {0: "P", 1: "C", 2: "B", 3: "U"}[prefix_bits]
        code = word & 0x3FFF
        dtcs.append(f"{prefix}{code:04X}")
    return dtcs

"""UDS (ISO-14229) protocol layer wrapping python-udsoncan.

Provides bidirectional diagnostic services:
  - ReadDataByIdentifier (0x22)
  - WriteDataByIdentifier (0x2E)
  - DiagnosticSessionControl (0x10)
  - ECUReset (0x11)
  - SecurityAccess (0x27)
  - RoutineControl (0x31) — start/stop/query routines
  - ReadDTCInformation (0x19)
  - ClearDiagnosticInformation (0x14)
  - InputOutputControlByIdentifier (0x2F)

Requires a J2534 or SocketCAN transport (not ELM327).
"""

from __future__ import annotations

import asyncio
from typing import Any

import udsoncan
from udsoncan.client import Client
from udsoncan.connections import BaseConnection
from udsoncan.exceptions import TimeoutException
from udsoncan.services import (
    DiagnosticSessionControl,
    ECUReset,
    RoutineControl,
)

from obd2_mcp.config import settings
from obd2_mcp.transport.base import BaseTransport


class _RawDidCodec(udsoncan.DidCodec):
    """Passthrough codec: decode any DID to its raw bytes.

    A generic scan tool reads arbitrary DIDs (VIN, part numbers, serials) without
    OEM-specific definitions, so we register this as the ``default`` codec and let
    the caller interpret the bytes (ASCII / hex) as appropriate.
    """

    def encode(self, val: Any) -> bytes:
        return bytes(val)

    def decode(self, payload: bytes) -> bytes:
        return payload

    def __len__(self) -> int:
        raise udsoncan.DidCodec.ReadAllRemainingData


class _TransportConnection(BaseConnection):
    """Adapter that bridges python-udsoncan's BaseConnection to our transport."""

    def __init__(
        self, transport: BaseTransport, loop: asyncio.AbstractEventLoop,
        timeout: float = 5.0,
    ) -> None:
        super().__init__("obd2-ai-tool")
        self._transport = transport
        # Per-request wait for the combined ISO-TP send_raw. udsoncan's own
        # request_timeout doesn't cover this because our transport does the
        # request+response in one blocking call inside specific_send().
        self._timeout = timeout
        # The loop that owns the transport. udsoncan drives this connection from
        # an executor thread (run_in_executor), so we must hand work back to that
        # loop with run_coroutine_threadsafe rather than run_until_complete.
        self._loop = loop
        # Our transport's send_raw() does a combined ISO-TP request+response,
        # but udsoncan calls specific_send() then specific_wait_frame() in two
        # steps. We buffer the response from the send so wait can return it.
        self._rxbuf: bytes | None = None

    def _run(self, coro, timeout: float | None = None) -> bytes:
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def is_open(self) -> bool:
        # The underlying transport owns the real connection lifecycle; this
        # adapter is only ever used while the transport is already connected.
        return getattr(self._transport, "is_connected", True)

    def specific_send(self, payload: bytes) -> None:
        # send_raw returns the full UDS response; stash it for wait_frame().
        self._rxbuf = self._run(
            self._transport.send_raw(
                payload,
                tx_id=settings.uds_tx_id,
                rx_id=settings.uds_rx_id,
                timeout=self._timeout,
            )
        )

    def specific_wait_frame(self, timeout: int | None = None) -> bytes:
        if self._rxbuf is None:
            raise TimeoutException("No UDS response buffered from send")
        resp, self._rxbuf = self._rxbuf, None
        return resp

    def empty_rxqueue(self) -> None:
        self._rxbuf = None

    def empty_txqueue(self) -> None:
        pass


class UDSProtocol:
    """High-level UDS service methods."""

    # Default client config — override per vehicle via kwargs
    DEFAULT_CONFIG: dict[str, Any] = {
        "exception_on_negative_response": True,
        "exception_on_invalid_response": True,
        "exception_on_unexpected_response": True,
        "security_algo": None,
        "tolerate_zero_padding": True,
        "ignore_all_zero_dtc": True,
        "dtc_severity_availability_mask": 0xFF,
        "server_address_format": None,
        "server_memorysize_format": None,
        "data_identifiers": {"default": _RawDidCodec},
        "input_output": {},
    }

    def __init__(self, transport: BaseTransport) -> None:
        self._transport = transport

    def _make_client(
        self, loop: asyncio.AbstractEventLoop, timeout: float = 5.0
    ) -> Client:
        conn = _TransportConnection(self._transport, loop, timeout=timeout)
        return Client(conn, request_timeout=timeout, config=self.DEFAULT_CONFIG.copy())

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def change_session(self, session: int = 0x03) -> dict[str, Any]:
        """Switch diagnostic session.

        Common sessions:
          0x01 = defaultSession
          0x02 = programmingSession
          0x03 = extendedDiagnosticSession
        """
        loop = asyncio.get_event_loop()

        def _do() -> dict[str, Any]:
            with self._make_client(loop) as client:
                response = client.change_session(session)
                return {
                    "session": hex(session),
                    "positive": response.positive,
                    "raw": response.original_payload.hex() if response.original_payload else None,
                }

        return await loop.run_in_executor(None, _do)

    # ------------------------------------------------------------------
    # ECU Reset
    # ------------------------------------------------------------------

    async def ecu_reset(self, reset_type: int = 0x01) -> dict[str, Any]:
        """Reset the ECU.

        reset_type:
          0x01 = hardReset
          0x02 = keyOffOnReset
          0x03 = softReset
        """
        loop = asyncio.get_event_loop()

        def _do() -> dict[str, Any]:
            with self._make_client(loop) as client:
                response = client.ecu_reset(reset_type)
                return {"reset_type": hex(reset_type), "positive": response.positive}

        return await loop.run_in_executor(None, _do)

    # ------------------------------------------------------------------
    # Read Data By Identifier (0x22)
    # ------------------------------------------------------------------

    async def read_data_by_id(self, did: int) -> dict[str, Any]:
        """Read a DID (Data Identifier) value.

        Common DIDs:
          0xF190 = VIN
          0xF18C = ECU Serial Number
          0xF187 = Spare Part Number
          0xF80A = Calibration ID
        """
        loop = asyncio.get_event_loop()

        from obd2_mcp.config import settings

        def _do() -> dict[str, Any]:
            with self._make_client(loop, timeout=settings.obd_read_timeout) as client:
                response = client.read_data_by_identifier(did)
                raw = response.original_payload
                return {
                    "did": hex(did),
                    "positive": response.positive,
                    "raw_hex": raw.hex() if raw else None,
                    "data": response.service_data.values.get(did) if response.positive else None,
                }

        return await loop.run_in_executor(None, _do)

    # ------------------------------------------------------------------
    # Routine Control (0x31)
    # ------------------------------------------------------------------

    async def start_routine(self, routine_id: int, data: bytes = b"") -> dict[str, Any]:
        """Start a diagnostic routine.

        Common routines vary by OEM. Examples:
          0x0203 = Erase Memory (some GM)
          0xFF00 = Check Programming Dependencies
          0x0100 = Erase Flash
        """
        loop = asyncio.get_event_loop()

        def _do() -> dict[str, Any]:
            with self._make_client(loop, timeout=30.0) as client:
                response = client.start_routine(routine_id, data=data)
                raw = response.original_payload
                return {
                    "routine_id": hex(routine_id),
                    "positive": response.positive,
                    "raw_hex": raw.hex() if raw else None,
                }

        return await loop.run_in_executor(None, _do)

    async def stop_routine(self, routine_id: int) -> dict[str, Any]:
        loop = asyncio.get_event_loop()

        def _do() -> dict[str, Any]:
            with self._make_client(loop) as client:
                response = client.stop_routine(routine_id)
                return {"routine_id": hex(routine_id), "positive": response.positive}

        return await loop.run_in_executor(None, _do)

    async def get_routine_result(self, routine_id: int) -> dict[str, Any]:
        loop = asyncio.get_event_loop()

        def _do() -> dict[str, Any]:
            with self._make_client(loop) as client:
                response = client.request_routine_results(routine_id)
                raw = response.original_payload
                return {
                    "routine_id": hex(routine_id),
                    "positive": response.positive,
                    "raw_hex": raw.hex() if raw else None,
                }

        return await loop.run_in_executor(None, _do)

    # ------------------------------------------------------------------
    # Read DTC Information (0x19)
    # ------------------------------------------------------------------

    async def read_uds_dtcs(
        self, sub_function: int = 0x02, status_mask: int = 0xFF
    ) -> list[dict[str, Any]]:
        """Read DTCs via UDS Service 0x19 ReadDTCInformation.

        sub_function 0x02 = reportDTCByStatusMask (most common)
        """
        loop = asyncio.get_event_loop()

        def _do() -> list[dict[str, Any]]:
            with self._make_client(loop) as client:
                response = client.get_dtc_by_status_mask(status_mask)
                dtcs = []
                if response.positive and response.service_data:
                    for dtc in response.service_data.dtcs:
                        dtcs.append({
                            "id": hex(dtc.id),
                            "status": hex(dtc.status.byte_val),
                            "severity": hex(dtc.severity.byte_val) if dtc.severity else None,
                        })
                return dtcs

        return await loop.run_in_executor(None, _do)

    # ------------------------------------------------------------------
    # Clear Diagnostic Information (0x14)
    # ------------------------------------------------------------------

    async def clear_uds_dtcs(self, group_of_dtc: int = 0xFFFFFF) -> dict[str, Any]:
        """Clear DTCs via UDS. 0xFFFFFF = all DTCs."""
        loop = asyncio.get_event_loop()

        def _do() -> dict[str, Any]:
            with self._make_client(loop) as client:
                response = client.clear_dtc(group_of_dtc)
                return {"group": hex(group_of_dtc), "positive": response.positive}

        return await loop.run_in_executor(None, _do)

    # ------------------------------------------------------------------
    # Security Access (0x27)
    # ------------------------------------------------------------------

    async def security_access(
        self, level: int, security_algo: Any | None = None
    ) -> dict[str, Any]:
        """Unlock a security level.

        Requires a security algorithm (seed→key function) specific to the OEM/ECU.
        Without a valid algorithm this will fail at the key step.
        """
        loop = asyncio.get_event_loop()

        def _do() -> dict[str, Any]:
            cfg = self.DEFAULT_CONFIG.copy()
            if security_algo:
                cfg["security_algo"] = security_algo
            conn = _TransportConnection(self._transport, loop)
            with Client(conn, request_timeout=5.0, config=cfg) as client:
                response = client.unlock_security_access(level)
                return {"level": hex(level), "positive": response.positive}

        return await loop.run_in_executor(None, _do)

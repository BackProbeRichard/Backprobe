"""DoIP (Diagnostics over IP — ISO 13400-2) transport over TCP.

Used on Ethernet-capable vehicles:
  - BMW (F/G/i series), MINI post-2018
  - VW/Audi MEB platform (ID.4, e-tron, etc.)
  - Mercedes-Benz post-2019
  - GM Ultium / EV platforms
  - Most new vehicles using OBD-II DoIP adapters

DoIP tunnels standard UDS requests inside an IP wrapper.  The vehicle
exposes a TCP gateway on port 13400.  This transport handles:
  1. TCP connect
  2. Routing Activation (ISO 13400-2 §9.3)
  3. Wrapping each UDS call in a Diagnostic Message (payload type 0x8001)
  4. Parsing the ACK (0x8002) + UDS response from a second Diagnostic Message

Compatible hardware:
  - Autel Maxiflash VCMI (DoIP mode via USB-C to Ethernet OBD cable)
  - Any DoIP pass-thru gateway / OBD-to-Ethernet adapter

Typical gateway IPs:
  - 169.254.x.x  — link-local (direct USB-to-Ethernet OBD adapter)
  - 192.168.x.x  — vehicle-assigned static IP (check vehicle-specific docs)
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

from obd2_mcp.config import settings
from obd2_mcp.transport.base import BaseTransport, TransportStatus

# ── DoIP header ───────────────────────────────────────────────────────────────
_PROTOCOL_VERSION     = 0x02   # ISO 13400-2:2012
_PROTOCOL_VERSION_INV = 0xFD   # bitwise inverse
_HEADER_LEN           = 8      # version(1) + inv(1) + type(2) + length(4)

# ── Payload types ─────────────────────────────────────────────────────────────
_ROUTING_ACTIVATION_REQUEST  = 0x0005
_ROUTING_ACTIVATION_RESPONSE = 0x0006
_DIAGNOSTIC_MESSAGE          = 0x8001
_DIAGNOSTIC_MESSAGE_POS_ACK  = 0x8002
_DIAGNOSTIC_MESSAGE_NEG_ACK  = 0x8003

# ── Routing activation ────────────────────────────────────────────────────────
_ACTIVATION_TYPE_DEFAULT = 0x00
_ROUTING_SUCCESS         = 0x10   # "Routing successfully activated"

# ── NACK reason codes ─────────────────────────────────────────────────────────
_NACK_REASON = {
    0x00: "Incorrect pattern format",
    0x01: "Unknown payload type",
    0x02: "Message too large",
    0x03: "Out of memory",
    0x04: "Invalid payload length",
}


# ── Frame builders ────────────────────────────────────────────────────────────

def _pack_header(payload_type: int, payload: bytes) -> bytes:
    # ">BBHI" = version(u8) + inv(u8) + type(u16) + length(u32)
    return struct.pack(">BBHI", _PROTOCOL_VERSION, _PROTOCOL_VERSION_INV,
                       payload_type, len(payload)) + payload


def _routing_activation_request(source_addr: int) -> bytes:
    # source_addr(u16) + activation_type(u8) + reserved(u32)
    payload = struct.pack(">HBI", source_addr, _ACTIVATION_TYPE_DEFAULT, 0)
    return _pack_header(_ROUTING_ACTIVATION_REQUEST, payload)


def _diagnostic_message(source_addr: int, target_addr: int, uds_data: bytes) -> bytes:
    payload = struct.pack(">HH", source_addr, target_addr) + uds_data
    return _pack_header(_DIAGNOSTIC_MESSAGE, payload)


# ── Transport ─────────────────────────────────────────────────────────────────

class DoIPTransport(BaseTransport):
    """ISO 13400-2 DoIP transport over TCP/IP.

    Connects to the vehicle's Ethernet DoIP gateway and tunnels UDS.
    The existing UDSProtocol layer works unchanged on top of this transport.

    Usage::

        transport = DoIPTransport(host="169.254.0.10")
        await transport.connect()          # TCP connect + routing activation
        response = await transport.send_raw(
            bytes([0x22, 0xF1, 0x90]),     # ReadDataByIdentifier — VIN
            tx_id=0x0E00,                  # tester logical address
            rx_id=0x0010,                  # ECU logical address
        )
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        source_addr: int | None = None,
        target_addr: int | None = None,
    ) -> None:
        super().__init__()
        self._host        = host        or settings.doip_host
        self._port        = port        or settings.doip_port
        self._source_addr = source_addr if source_addr is not None else settings.doip_source_addr
        self._target_addr = target_addr if target_addr is not None else settings.doip_target_addr

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # Serialise requests — DoIP is a single-connection protocol
        self._lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._status = TransportStatus.CONNECTING
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=10.0,
            )
            await self._routing_activation()
            self._status = TransportStatus.CONNECTED
            self._device_info = {
                "transport":   "doip",
                "host":        self._host,
                "port":        self._port,
                "source_addr": hex(self._source_addr),
                "target_addr": hex(self._target_addr),
            }
        except Exception as exc:
            self._status = TransportStatus.ERROR
            await self._close_socket()
            raise RuntimeError(f"DoIP connect failed: {exc}") from exc

    async def _routing_activation(self) -> None:
        assert self._reader and self._writer

        req = _routing_activation_request(self._source_addr)
        self._writer.write(req)
        await self._writer.drain()

        hdr = await asyncio.wait_for(self._reader.readexactly(_HEADER_LEN), timeout=5.0)
        _ver, _inv, payload_type, length = struct.unpack(">BBHI", hdr)

        if payload_type != _ROUTING_ACTIVATION_RESPONSE:
            raise RuntimeError(
                f"Expected routing activation response (0x0006), "
                f"got payload type {payload_type:#06x}"
            )

        payload = await asyncio.wait_for(self._reader.readexactly(length), timeout=5.0)
        # Byte 4 of the response payload is the response code
        code = payload[4] if len(payload) > 4 else 0xFF
        if code != _ROUTING_SUCCESS:
            raise RuntimeError(
                f"DoIP routing activation rejected (code {code:#04x}). "
                "Vehicle may require OEM authentication or a specific activation type."
            )

    async def disconnect(self) -> None:
        await self._close_socket()
        self._status = TransportStatus.DISCONNECTED

    async def _close_socket(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    # ── send/receive ──────────────────────────────────────────────────────────

    async def send_raw(
        self, data: bytes, tx_id: int, rx_id: int, timeout: float = 5.0
    ) -> bytes:
        """Wrap a UDS request in a DoIP DiagnosticMessage and return the response.

        Args:
            data:    Raw UDS payload (service ID + params).
            tx_id:   Tester logical address (0 = use default from settings).
            rx_id:   ECU logical address (0 = use default from settings).
            timeout: Seconds to wait for UDS response.

        Returns:
            Raw UDS response bytes (service ID + data), DoIP header stripped.
        """
        if not self.is_connected or not self._writer or not self._reader:
            raise RuntimeError("DoIP transport is not connected.")

        src = tx_id or self._source_addr
        tgt = rx_id or self._target_addr

        async with self._lock:
            msg = _diagnostic_message(src, tgt, data)
            self._writer.write(msg)
            await self._writer.drain()

            deadline = asyncio.get_event_loop().time() + timeout

            # Read response frames.  Expect:
            #   1. DiagnosticMessagePositiveACK  (0x8002) — gateway confirmed receipt
            #   2. DiagnosticMessage             (0x8001) — actual UDS response
            # Some gateways skip the ACK and send the response directly.
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"DoIP: no UDS response from {self._host} within {timeout}s"
                    )

                hdr = await asyncio.wait_for(
                    self._reader.readexactly(_HEADER_LEN),
                    timeout=remaining,
                )
                _ver, _inv, payload_type, length = struct.unpack(">BBHI", hdr)

                remaining = deadline - asyncio.get_event_loop().time()
                payload = await asyncio.wait_for(
                    self._reader.readexactly(length),
                    timeout=max(0.5, remaining),
                )

                if payload_type == _DIAGNOSTIC_MESSAGE_NEG_ACK:
                    code = payload[4] if len(payload) > 4 else 0xFF
                    raise RuntimeError(
                        f"DoIP diagnostic NACK: {_NACK_REASON.get(code, hex(code))}"
                    )

                if payload_type == _DIAGNOSTIC_MESSAGE_POS_ACK:
                    # ACK from gateway — keep waiting for the ECU's UDS response
                    continue

                if payload_type == _DIAGNOSTIC_MESSAGE:
                    # Strip 4-byte source_addr + target_addr prefix → raw UDS bytes
                    return payload[4:]

    async def list_devices(self) -> list[dict[str, Any]]:
        """Return the configured DoIP gateway as a single-item list."""
        return [{
            "transport":   "doip",
            "host":        self._host,
            "port":        self._port,
            "source_addr": hex(self._source_addr),
            "target_addr": hex(self._target_addr),
        }]

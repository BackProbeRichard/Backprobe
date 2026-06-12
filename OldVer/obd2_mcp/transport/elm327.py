"""ELM327 transport wrapping the python-obd library.

Use this for standard ELM327 USB/Bluetooth/Wi-Fi adapters.
For professional J2534 devices (Opus IVS, Autel VCMI) use J2534Transport instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import obd

from obd2_mcp.config import settings
from obd2_mcp.transport.base import BaseTransport, TransportStatus


class ELM327Transport(BaseTransport):
    """python-obd backed transport for ELM327 adapters."""

    def __init__(
        self,
        port: str | None = None,
        baudrate: int | None = None,
        fast: bool | None = None,
        timeout: float | None = None,
    ) -> None:
        super().__init__()
        self._port = port or settings.elm327_port
        self._baudrate = baudrate or settings.elm327_baudrate
        self._fast = fast if fast is not None else settings.elm327_fast
        self._timeout = timeout or settings.elm327_timeout
        self._conn: obd.OBD | None = None

    async def connect(self) -> None:
        self._status = TransportStatus.CONNECTING
        loop = asyncio.get_event_loop()
        try:
            def _connect() -> obd.OBD:
                return obd.OBD(
                    portstr=self._port,
                    baudrate=self._baudrate,
                    fast=self._fast,
                    timeout=self._timeout,
                )

            self._conn = await loop.run_in_executor(None, _connect)
            if self._conn.status() == obd.OBDStatus.NOT_CONNECTED:
                raise RuntimeError("python-obd could not connect to any ELM327 device.")

            self._device_info = {
                "port": self._conn.port_name(),
                "protocol": str(self._conn.protocol_name()),
                "status": str(self._conn.status()),
            }
            self._status = TransportStatus.CONNECTED
        except Exception as exc:
            self._status = TransportStatus.ERROR
            raise RuntimeError(f"ELM327 connect failed: {exc}") from exc

    async def disconnect(self) -> None:
        if self._conn is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._conn.close)
            self._conn = None
        self._status = TransportStatus.DISCONNECTED

    def get_obd_connection(self) -> obd.OBD | None:
        return self._conn

    async def send_raw(self, data: bytes, tx_id: int, rx_id: int, timeout: float = 5.0) -> bytes:
        """ELM327 doesn't support true raw CAN framing.

        This method is provided for interface compatibility.
        For raw UDS over ELM327 you can use AT commands, but it's unreliable —
        prefer J2534Transport for UDS bidirectional tests.
        """
        raise NotImplementedError(
            "ELM327Transport does not support raw ISO-TP send_raw. "
            "Use J2534Transport for UDS/bidirectional tests."
        )

    async def list_devices(self) -> list[dict[str, Any]]:
        """Return available serial ports that look like ELM327 devices."""
        import serial.tools.list_ports  # type: ignore[import]

        ports = []
        for p in serial.tools.list_ports.comports():
            ports.append({
                "port": p.device,
                "description": p.description,
                "hwid": p.hwid,
            })
        return ports

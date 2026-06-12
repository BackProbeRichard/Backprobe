"""SocketCAN transport for Linux environments.

Wraps python-can's socketcan interface with ISO-TP framing.
Useful for headless shop servers or Raspberry Pi setups.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from obd2_mcp.config import settings
from obd2_mcp.transport.base import BaseTransport, TransportStatus


class SocketCANTransport(BaseTransport):
    """Linux SocketCAN transport using python-can + python-can-isotp."""

    def __init__(self, channel=None, bitrate=None, can_fd=None, fd_data_rate=None):
        super().__init__()
        self._channel = channel or settings.socketcan_channel
        self._bitrate = bitrate or settings.socketcan_bitrate
        self._can_fd = can_fd if can_fd is not None else settings.can_fd
        self._fd_data_rate = fd_data_rate or settings.can_fd_data_rate
        self._bus = None

    async def connect(self):
        if platform.system() != "Linux":
            raise RuntimeError("SocketCAN transport is only supported on Linux.")
        self._status = TransportStatus.CONNECTING
        try:
            import can
            loop = asyncio.get_event_loop()
            if self._can_fd:
                self._bus = await loop.run_in_executor(
                    None,
                    lambda: can.Bus(
                        interface="socketcan",
                        channel=self._channel,
                        bitrate=self._bitrate,
                        fd=True,
                        data_bitrate=self._fd_data_rate,
                    ),
                )
                self._device_info = {
                    "channel": self._channel,
                    "bitrate": self._bitrate,
                    "can_fd": True,
                    "fd_data_rate": self._fd_data_rate,
                }
            else:
                self._bus = await loop.run_in_executor(
                    None,
                    lambda: can.Bus(
                        interface="socketcan",
                        channel=self._channel,
                        bitrate=self._bitrate,
                    ),
                )
                self._device_info = {"channel": self._channel, "bitrate": self._bitrate, "can_fd": False}
            self._status = TransportStatus.CONNECTED
        except Exception as exc:
            self._status = TransportStatus.ERROR
            raise RuntimeError(f"SocketCAN connect failed: {exc}") from exc

    async def disconnect(self):
        if self._bus is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._bus.shutdown)
            self._bus = None
        self._status = TransportStatus.DISCONNECTED

    def get_raw_bus(self):
        return self._bus

    async def send_raw(self, data, tx_id, rx_id, timeout=5.0):
        if not self.is_connected or self._bus is None:
            raise RuntimeError("SocketCAN transport is not connected.")
        import isotp, time
        addr = isotp.Address(
            isotp.AddressingMode.Normal_11bits, txid=tx_id, rxid=rx_id,
        )
        stack = isotp.CanStack(self._bus, address=addr)
        loop = asyncio.get_event_loop()
        def _exchange():
            stack.send(data)
            deadline = time.monotonic() + timeout
            while True:
                stack.process()
                if stack.available():
                    return stack.recv()
                if time.monotonic() > deadline:
                    raise TimeoutError(f"No ISO-TP response from 0x{rx_id:03X} within {timeout}s")
                time.sleep(0.001)
        return await loop.run_in_executor(None, _exchange)

    async def list_devices(self):
        if platform.system() != "Linux":
            return []
        import subprocess
        result = subprocess.run(
            ["ip", "link", "show", "type", "can"],
            capture_output=True, text=True, timeout=3,
        )
        ifaces = []
        for line in result.stdout.splitlines():
            if ": " in line and "@" not in line:
                parts = line.split(": ", 1)
                if parts:
                    name = parts[0].lstrip("0123456789").strip()
                    if name:
                        ifaces.append({"channel": name})
        return ifaces

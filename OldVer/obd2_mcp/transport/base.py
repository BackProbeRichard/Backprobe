"""Abstract base class for all transport implementations."""

from __future__ import annotations

import abc
from enum import Enum
from typing import Any


class TransportStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class BaseTransport(abc.ABC):
    """Common interface for all transport backends.

    Concrete subclasses wrap ELM327 (python-obd), J2534 (python-can j2534),
    SocketCAN (python-can socketcan), or a virtual/mock backend.
    """

    def __init__(self) -> None:
        self._status = TransportStatus.DISCONNECTED
        self._device_info: dict[str, Any] = {}

    @property
    def status(self) -> TransportStatus:
        return self._status

    @property
    def device_info(self) -> dict[str, Any]:
        return self._device_info

    @property
    def is_connected(self) -> bool:
        return self._status == TransportStatus.CONNECTED

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the connection to the hardware device."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close the connection gracefully."""

    # ------------------------------------------------------------------
    # Raw CAN / ISO-TP
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def send_raw(self, data: bytes, tx_id: int, rx_id: int, timeout: float = 5.0) -> bytes:
        """Send a raw ISO-TP payload and return the response.

        Args:
            data: UDS or raw payload bytes.
            tx_id: CAN arbitration ID to transmit on.
            rx_id: Expected response arbitration ID.
            timeout: Seconds to wait for a response.

        Returns:
            Response bytes from the ECU.
        """

    # ------------------------------------------------------------------
    # python-obd integration (Mode 01/02/03/04 PIDs)
    # Only implemented by transports that support the ELM327 AT command set.
    # J2534 and SocketCAN transports bypass this layer and use raw CAN.
    # ------------------------------------------------------------------

    def get_obd_connection(self) -> Any:
        """Return a python-obd OBD connection object if available, else None."""
        return None

    def get_raw_bus(self) -> Any:
        """Return the underlying python-can Bus object, if available.

        Used by CanSnifferProtocol to attach a Notifier for passive frame capture.
        Returns None for transports that don't expose a raw CAN bus (e.g. ELM327).
        """
        return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def list_devices(self) -> list[dict[str, Any]]:
        """Return a list of available devices of this transport type."""

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(status={self._status.value})"

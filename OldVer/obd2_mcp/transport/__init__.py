"""Transport layer — hardware abstraction over J2534, ELM327, SocketCAN."""

from obd2_mcp.transport.base import BaseTransport, TransportStatus
from obd2_mcp.transport.elm327 import ELM327Transport
from obd2_mcp.transport.j2534 import J2534Transport
from obd2_mcp.transport.socketcan import SocketCANTransport
from obd2_mcp.transport.virtual import VirtualTransport

__all__ = [
    "BaseTransport",
    "TransportStatus",
    "ELM327Transport",
    "J2534Transport",
    "SocketCANTransport",
    "VirtualTransport",
]

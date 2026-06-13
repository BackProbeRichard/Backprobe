"""TIER 1 — transports move bytes, nothing more.

The interface lives in base.py; backends implement it. Import the
vocabulary from here:

    from backprobe.transport import Session, Channel, ConnectProfile, ...
"""

from backprobe.transport.base import (
    Channel,
    ConnectError,
    ConnectProfile,
    Device,
    DeviceBusy,
    DeviceLost,
    DeviceReport,
    InternalError,
    NotSupported,
    Reply,
    Session,
    Timeout,
    TransportBackend,
    TransportError,
)

__all__ = [
    "Channel",
    "ConnectError",
    "ConnectProfile",
    "Device",
    "DeviceBusy",
    "DeviceLost",
    "DeviceReport",
    "InternalError",
    "NotSupported",
    "Reply",
    "Session",
    "Timeout",
    "TransportBackend",
    "TransportError",
]

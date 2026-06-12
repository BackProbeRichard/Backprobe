"""The transport interface.

Every transport backend (J2534, virtual, future kinds) implements these
types. The daemon speaks only this vocabulary — it never sees a J2534 call,
and raw J2534 return codes never escape this layer.

Ownership chain (each level "has" the next):

    TransportBackend  →  Device  →  Session  →  Channel

This module is pure types: dataclasses, ABCs, and exceptions. No logic,
no I/O, no imports beyond the standard library.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


# ─── Errors ──────────────────────────────────────────────────────────────
# The only exceptions the transport layer may raise.


class TransportError(Exception):
    """Base for every transport-layer error."""


class DeviceBusy(TransportError):
    """Another program owns the device (wiTECH, FDRS, ...)."""


class DeviceLost(TransportError):
    """Device unplugged or powered off."""


class ConnectError(TransportError):
    """This connection profile didn't take."""


class Timeout(TransportError):
    """No matching reply within the time given."""


class NotSupported(TransportError):
    """Device or vehicle lacks the requested feature."""


class InternalError(TransportError):
    """Our bug, or a loaded-but-broken DLL."""


# ─── Data carriers (immutable) ───────────────────────────────────────────


@dataclass(frozen=True)
class Device:
    """One discovered device. Returned by enumerate(), passed to open()."""

    vendor: str
    name: str
    address: str  # how the backend reaches it (J2534: DLL path)


@dataclass(frozen=True)
class DeviceReport:
    """Identity card of a held device, captured at open."""

    vendor: str
    name: str
    address: str
    firmware: str | None
    dll_version: str | None
    api_version: str | None


@dataclass(frozen=True)
class ConnectProfile:
    """One connection attempt's parameters. The probe is a loop over these."""

    protocol: str  # Phase 1: "ISO15765"
    bitrate: int  # 500000 | 250000
    addressing: str  # "11-bit" | "29-bit"


@dataclass(frozen=True)
class Reply:
    """One ECU's answer to a request."""

    ecu: int  # source address, e.g. 0x7E8
    payload: bytes  # response bytes, CAN-ID prefix already stripped


# ─── The three behavioral objects ────────────────────────────────────────


class TransportBackend(ABC):
    """A KIND of transport. Phase 1 has two: J2534Backend, VirtualBackend."""

    @abstractmethod
    def enumerate(self) -> list[Device]:
        """Discover devices of this kind. No hardware touched.

        Never raises — returns [] if none installed; a dead entry
        (e.g. registry points at a missing DLL) is skipped and logged.
        """

    @abstractmethod
    def open(self, device: Device) -> "Session":
        """Take exclusive ownership of one device. One attempt, no waiting.

        Raises DeviceBusy | DeviceLost | InternalError.
        """


class Session(ABC):
    """One device, opened and exclusively held by us."""

    @abstractmethod
    def info(self) -> DeviceReport:
        """Identity card of the held device. Data captured at open; never raises."""

    @abstractmethod
    def read_voltage(self) -> int:
        """Pin-16 battery voltage, in millivolts. The vehicle-presence signal.

        Raises DeviceLost | NotSupported.
        """

    @abstractmethod
    def connect(self, profile: ConnectProfile) -> "Channel":
        """Attempt a vehicle connection with exactly one profile. One attempt.

        Raises ConnectError | DeviceLost.
        """

    @abstractmethod
    def close(self) -> None:
        """Release the device, completely and unconditionally. Idempotent.

        Never raises — failures are logged and swallowed. After close,
        another program's open must succeed immediately.
        """


class Channel(ABC):
    """A live protocol connection to a vehicle through a held device."""

    @abstractmethod
    def ask(self, request: bytes, timeout: float) -> Reply:
        """Send one request, return the first matching reply.

        TX echoes, ISO15765 first-frame markers, and stale replies from
        earlier timed-out requests are skipped internally.

        Raises Timeout | DeviceLost.
        """

    @abstractmethod
    def ask_all(self, request: bytes, window: float) -> list[Reply]:
        """Send one functional request, collect every ECU answering in the window.

        The census verb. [] is a valid answer, not an error.

        Raises DeviceLost.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down this protocol connection, keep the device held. Idempotent."""

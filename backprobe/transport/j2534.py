"""J2534 pass-thru transport — the real hardware backend (Phase 1 step 7).

Implements the base.py interface against a 32-bit stdcall J2534 DLL. All
design decisions are in PHASE_1_J2534_DESIGN.md; only the numbers are here.

  Decision 1 — injectable _J2534Lib seam; FakeJ2534Lib for Linux testing.
  Decision 2 — full 8-ECU FC filter set installed at connect time.
  Decision 3 — clear RX → write → read; SID/PID match as backstop.
  Decision 4 — central _J2534_TAXONOMY map; _check() converts return codes.
  Decision 5 — explicit argtypes/restype declared once in _RealJ2534Lib._bind().
  Decision 6 — LOOPBACK=0 only; BS/STMIN stay at DLL defaults.

The DLL is loaded lazily: importing this module on Linux does not touch
winreg or WinDLL. _RealJ2534Lib is only instantiated on Windows.
"""

from __future__ import annotations

import ctypes
import os
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backprobe.transport.virtual import VirtualVehicle

from backprobe import session_log
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
from backprobe.transport.constants import (
    CAN_29BIT_ID,
    CLEAR_MSG_FILTERS,
    CLEAR_PERIODIC_MSGS,
    CLEAR_RX_BUFFER,
    FLOW_CONTROL_FILTER,
    ISO15765,
    ISO15765_FRAME_PAD,
    LOOPBACK,
    OBD_FUNCTIONAL_11BIT,
    OBD_FUNCTIONAL_29BIT,
    READ_VBATT,
    REGISTRY_KEY_LIBRARY,
    REGISTRY_KEY_NAME,
    REGISTRY_KEY_VENDOR,
    REGISTRY_PATH,
    SET_CONFIG,
    START_OF_MESSAGE,
    STATUS_NOERROR,
    TX_MSG_TYPE,
    error_name,
)

# ─── ctypes structures (Decision 5) ─────────────────────────────────────────


class _SCONFIG(ctypes.Structure):
    _fields_ = [("Parameter", ctypes.c_ulong), ("Value", ctypes.c_ulong)]


class _SCONFIG_LIST(ctypes.Structure):
    _fields_ = [
        ("NumOfParams", ctypes.c_ulong),
        ("ConfigPtr", ctypes.POINTER(_SCONFIG)),
    ]


class _PASSTHRU_MSG(ctypes.Structure):
    _fields_ = [
        ("ProtocolID",     ctypes.c_ulong),
        ("RxStatus",       ctypes.c_ulong),
        ("TxFlags",        ctypes.c_ulong),
        ("Timestamp",      ctypes.c_ulong),
        ("DataSize",       ctypes.c_ulong),
        ("ExtraDataIndex", ctypes.c_ulong),
        ("Data",           ctypes.c_ubyte * 4128),
    ]


# ─── error taxonomy (Decision 4) ────────────────────────────────────────────
# Edit this dict to change how a J2534 return code maps to the exception the
# daemon sees. None = not an error (ERR_BUFFER_EMPTY = "no message yet").

_ERR_DEVICE_NOT_CONNECTED = 0x08
_ERR_DEVICE_IN_USE        = 0x0E
_ERR_BUFFER_EMPTY         = 0x10
_ERR_TIMEOUT              = 0x09  # PassThruWriteMsgs on mismatched bitrate

# After the first ECU reply, wait this long for additional ECUs before exiting
# ask_all early. 100 ms = 2× the J1979 P2 max (50 ms); any compliant ECU that
# will answer already has. Avoids burning up to 985 ms of silence per call on
# single-ECU vehicles.
_COALESCE_MS = 100

_J2534_TAXONOMY: dict[int, type[TransportError] | None] = {
    _ERR_DEVICE_IN_USE:        DeviceBusy,
    _ERR_DEVICE_NOT_CONNECTED: DeviceLost,
    0x01:                      NotSupported,  # ERR_NOT_SUPPORTED
    _ERR_BUFFER_EMPTY:         None,          # keep polling — not an error
    _ERR_TIMEOUT:              Timeout,       # write/read timeout → advance probe profile
}


def _check(ret: int, context: str, *, empty_ok: bool = False) -> bool:
    """Map a J2534 return code to a TransportError, or pass if ok.

    Returns True for STATUS_NOERROR, False for ERR_BUFFER_EMPTY (when
    empty_ok=True). Raises the taxonomy exception for everything else.
    """
    if ret == STATUS_NOERROR:
        return True
    if empty_ok and ret == _ERR_BUFFER_EMPTY:
        return False
    exc_type = _J2534_TAXONOMY.get(ret)
    name = error_name(ret)
    if exc_type is None:
        raise InternalError(f"{context}: {name} ({ret:#04x})")
    raise exc_type(f"{context}: {name} ({ret:#04x})")


def _mono_ms() -> int:
    return int(time.perf_counter() * 1000)


# ─── the injectable seam (Decision 1) ───────────────────────────────────────


class _J2534Lib(ABC):
    """Thin wrapper over the ~10 PassThru functions this backend uses.

    _RealJ2534Lib wraps a ctypes.WinDLL. FakeJ2534Lib backs it with
    VirtualVehicle encoders. All protocol logic in J2534Session / J2534Channel
    is agnostic to which is in use — that is the point of the seam.
    """

    @abstractmethod
    def open(self, name: str | None) -> int:
        """PassThruOpen. Returns device_id.
        Raises DeviceBusy | DeviceLost | InternalError."""

    @abstractmethod
    def close(self, device_id: int) -> None:
        """PassThruClose. Idempotent. Never raises."""

    @abstractmethod
    def get_version(self, device_id: int) -> tuple[str | None, str | None, str | None]:
        """Best-effort (firmware, dll, api). Never raises."""

    @abstractmethod
    def connect(self, device_id: int, protocol: int, flags: int, bitrate: int) -> int:
        """PassThruConnect. Returns channel_id. Raises ConnectError | DeviceLost."""

    @abstractmethod
    def disconnect(self, channel_id: int) -> None:
        """PassThruDisconnect. Idempotent. Never raises."""

    @abstractmethod
    def ioctl_teardown(self, channel_id: int) -> None:
        """CLEAR_PERIODIC_MSGS then CLEAR_MSG_FILTERS (sacred order). Never raises."""

    @abstractmethod
    def read_one(self, channel_id: int, timeout_ms: int) -> tuple[int, bytes] | None:
        """PassThruReadMsgs (one message at a time).
        Returns (rx_status, raw_bytes_with_4B_can_id_prefix) or None on
        ERR_BUFFER_EMPTY. Raises DeviceLost | InternalError."""

    @abstractmethod
    def write_msg(
        self, channel_id: int, data: bytes, tx_flags: int, timeout_ms: int
    ) -> None:
        """PassThruWriteMsgs (one message). Raises DeviceLost | InternalError."""

    @abstractmethod
    def start_filter(
        self,
        channel_id: int,
        filter_type: int,
        mask: bytes,
        pattern: bytes,
        flowctrl: bytes | None,
        *,
        tx_flags: int = 0,
    ) -> int:
        """PassThruStartMsgFilter. Returns filter_id.
        tx_flags: extra TxFlags bits for filter messages (e.g. CAN_29BIT_ID for 29-bit).
        Non-fatal: a filter warning is logged but does not abort connect."""

    @abstractmethod
    def ioctl_set_config(self, channel_id: int, params: dict[int, int]) -> None:
        """PassThruIoctl(SET_CONFIG). Raises DeviceLost | InternalError."""

    @abstractmethod
    def ioctl_clear_rx(self, channel_id: int) -> str | None:
        """PassThruIoctl(CLEAR_RX_BUFFER).
        Returns None if supported. Returns an error-name string if not supported
        (RX_CLEAR_UNSUPPORTED path; SID/PID matching compensates)."""

    @abstractmethod
    def ioctl_read_vbatt(self, device_id: int) -> int:
        """PassThruIoctl(READ_VBATT, device_id). Returns millivolts.
        Raises DeviceLost | NotSupported."""


# ─── real DLL wrapper (Decision 5: explicit argtypes/restype) ───────────────


class _RealJ2534Lib(_J2534Lib):
    """Wraps a ctypes.WinDLL. Only instantiated on Windows / 32-bit Python."""

    def __init__(self, dll_path: str) -> None:
        if os.name != "nt":
            raise InternalError(
                "J2534 DLL loading requires Windows 32-bit Python — "
                "use FakeJ2534Lib or VirtualBackend on Linux"
            )
        self._dll = ctypes.WinDLL(dll_path)  # type: ignore[attr-defined]
        self._bind()
        self._log = session_log.get_logger("J2534_LIB")

    def _bind(self) -> None:
        """Declare argtypes/restype for each PassThru function (Decision 5)."""
        ul   = ctypes.c_ulong
        pul  = ctypes.POINTER(ctypes.c_ulong)
        pmsg = ctypes.POINTER(_PASSTHRU_MSG)
        pvoid = ctypes.c_void_p
        cstr = ctypes.c_char_p
        cl   = ctypes.c_long

        bindings = [
            ("PassThruOpen",           [cstr,  pul],            cl),
            ("PassThruClose",          [ul],                    cl),
            ("PassThruConnect",        [ul, ul, ul, ul, pul],   cl),
            ("PassThruDisconnect",     [ul],                    cl),
            ("PassThruReadMsgs",       [ul, pmsg, pul, ul],     cl),
            ("PassThruWriteMsgs",      [ul, pmsg, pul, ul],     cl),
            ("PassThruStartMsgFilter", [ul, ul, pmsg, pmsg, pmsg, pul], cl),
            ("PassThruIoctl",          [ul, ul, pvoid, pvoid],  cl),
            ("PassThruGetLastError",   [cstr],                  cl),
            ("PassThruGetDllVersion",  [cstr, cstr, cstr],      cl),
        ]
        for name, argtypes, restype in bindings:
            fn = getattr(self._dll, name, None)
            if fn is not None:
                fn.argtypes = argtypes
                fn.restype  = restype

    # ─── _J2534Lib implementation ─────────────────────────────────────────

    def open(self, name: str | None) -> int:
        dev_id = ctypes.c_ulong(0)
        ret = self._dll.PassThruOpen(
            name.encode() if name else None, ctypes.byref(dev_id)
        )
        _check(ret, "PassThruOpen")
        return dev_id.value

    def close(self, device_id: int) -> None:
        try:
            self._dll.PassThruClose(ctypes.c_ulong(device_id))
        except Exception:
            pass

    def get_version(self, device_id: int) -> tuple[str | None, str | None, str | None]:
        fn = getattr(self._dll, "PassThruGetDllVersion", None)
        if fn is None:
            return None, None, None
        try:
            dll_buf = ctypes.create_string_buffer(80)
            api_buf = ctypes.create_string_buffer(80)
            fw_buf  = ctypes.create_string_buffer(80)
            ret = fn(dll_buf, api_buf, fw_buf)
            if ret == STATUS_NOERROR:
                def _s(b: ctypes.Array) -> str | None:  # type: ignore[type-arg]
                    v = b.value.decode("ascii", errors="replace").strip("\x00")
                    return v or None
                return _s(fw_buf), _s(dll_buf), _s(api_buf)
        except Exception:
            pass
        return None, None, None

    def connect(self, device_id: int, protocol: int, flags: int, bitrate: int) -> int:
        ch_id = ctypes.c_ulong(0)
        ret = self._dll.PassThruConnect(
            ctypes.c_ulong(device_id),
            ctypes.c_ulong(protocol),
            ctypes.c_ulong(flags),
            ctypes.c_ulong(bitrate),
            ctypes.byref(ch_id),
        )
        if ret != STATUS_NOERROR:
            raise ConnectError(f"PassThruConnect: {error_name(ret)} ({ret:#04x})")
        return ch_id.value

    def disconnect(self, channel_id: int) -> None:
        try:
            self._dll.PassThruDisconnect(ctypes.c_ulong(channel_id))
        except Exception:
            pass

    def ioctl_teardown(self, channel_id: int) -> None:
        ch = ctypes.c_ulong(channel_id)
        for ioctl_id in (CLEAR_PERIODIC_MSGS, CLEAR_MSG_FILTERS):
            try:
                self._dll.PassThruIoctl(ch, ctypes.c_ulong(ioctl_id), None, None)
            except Exception:
                pass

    def read_one(self, channel_id: int, timeout_ms: int) -> tuple[int, bytes] | None:
        msg = _PASSTHRU_MSG()
        num = ctypes.c_ulong(1)
        ret = self._dll.PassThruReadMsgs(
            ctypes.c_ulong(channel_id),
            ctypes.byref(msg),
            ctypes.byref(num),
            ctypes.c_ulong(max(1, timeout_ms)),
        )
        if not _check(ret, "PassThruReadMsgs", empty_ok=True):
            return None
        if num.value == 0:
            return None
        data = bytes(msg.Data[: msg.DataSize])
        return (msg.RxStatus, data)

    def write_msg(
        self, channel_id: int, data: bytes, tx_flags: int, timeout_ms: int
    ) -> None:
        msg = _PASSTHRU_MSG()
        msg.ProtocolID = ISO15765
        msg.TxFlags    = tx_flags
        msg.DataSize   = len(data)
        for i, b in enumerate(data):
            msg.Data[i] = b
        num = ctypes.c_ulong(1)
        ret = self._dll.PassThruWriteMsgs(
            ctypes.c_ulong(channel_id),
            ctypes.byref(msg),
            ctypes.byref(num),
            ctypes.c_ulong(max(1, timeout_ms)),
        )
        _check(ret, "PassThruWriteMsgs")

    def start_filter(
        self,
        channel_id: int,
        filter_type: int,
        mask: bytes,
        pattern: bytes,
        flowctrl: bytes | None,
        *,
        tx_flags: int = 0,
    ) -> int:
        def _msg(data: bytes) -> _PASSTHRU_MSG:
            m = _PASSTHRU_MSG()
            m.ProtocolID = ISO15765
            m.TxFlags    = ISO15765_FRAME_PAD | tx_flags
            m.DataSize   = len(data)
            for i, b in enumerate(data):
                m.Data[i] = b
            return m

        filter_id = ctypes.c_ulong(0)
        mask_msg    = _msg(mask)
        pattern_msg = _msg(pattern)
        if flowctrl is not None:
            fc_msg = _msg(flowctrl)
            ret = self._dll.PassThruStartMsgFilter(
                ctypes.c_ulong(channel_id),
                ctypes.c_ulong(filter_type),
                ctypes.byref(mask_msg),
                ctypes.byref(pattern_msg),
                ctypes.byref(fc_msg),
                ctypes.byref(filter_id),
            )
        else:
            ret = self._dll.PassThruStartMsgFilter(
                ctypes.c_ulong(channel_id),
                ctypes.c_ulong(filter_type),
                ctypes.byref(mask_msg),
                ctypes.byref(pattern_msg),
                None,
                ctypes.byref(filter_id),
            )
        if ret != STATUS_NOERROR:
            self._log.event("FILTER_WARN", code=error_name(ret))
        return filter_id.value

    def ioctl_set_config(self, channel_id: int, params: dict[int, int]) -> None:
        n   = len(params)
        cfg = (_SCONFIG * n)()
        for i, (param, val) in enumerate(params.items()):
            cfg[i] = _SCONFIG(Parameter=param, Value=val)
        cfglist = _SCONFIG_LIST(NumOfParams=n, ConfigPtr=cfg)
        ret = self._dll.PassThruIoctl(
            ctypes.c_ulong(channel_id),
            ctypes.c_ulong(SET_CONFIG),
            ctypes.cast(ctypes.byref(cfglist), ctypes.c_void_p),
            None,
        )
        _check(ret, "PassThruIoctl(SET_CONFIG)")

    def ioctl_clear_rx(self, channel_id: int) -> str | None:
        ret = self._dll.PassThruIoctl(
            ctypes.c_ulong(channel_id),
            ctypes.c_ulong(CLEAR_RX_BUFFER),
            None,
            None,
        )
        return None if ret == STATUS_NOERROR else error_name(ret)

    def ioctl_read_vbatt(self, device_id: int) -> int:
        mv = ctypes.c_ulong(0)
        ret = self._dll.PassThruIoctl(
            ctypes.c_ulong(device_id),
            ctypes.c_ulong(READ_VBATT),
            None,
            ctypes.cast(ctypes.byref(mv), ctypes.c_void_p),
        )
        _check(ret, "PassThruIoctl(READ_VBATT)")
        return mv.value


# ─── fake DLL (Decision 1 — Linux-testable logic) ───────────────────────────


class FakeJ2534Lib(_J2534Lib):
    """Fake J2534 DLL backed by a VirtualVehicle preset.

    write_msg routes the OBD request to the vehicle's ECUs via the virtual
    backend's _respond() encoder and queues the responses for read_one. This
    makes every J2534Channel decision (echo skip, first-frame skip, SID/PID
    backstop, filter install, teardown order) provable on Linux without hardware.

    vbatt_mv: simulated pin-16 voltage. Adjust in tests to probe voltage paths.
    _noop_delay_ms: how long read_one sleeps when the queue is empty. Default
        5 ms approximates real DLL latency; set to 0 for fast tests.
    """

    def __init__(
        self,
        vehicle: VirtualVehicle,
        *,
        vbatt_mv: int = 12_600,
        _noop_delay_ms: float = 5.0,
    ) -> None:
        self._vehicle = vehicle
        self._vbatt_mv = vbatt_mv
        self._noop_delay = _noop_delay_ms / 1000.0
        self._rx_queue: list[tuple[int, bytes]] = []  # (rx_status, raw_with_can_id)

    # ─── _J2534Lib implementation ─────────────────────────────────────────

    def open(self, name: str | None) -> int:
        return 1  # fake device_id

    def close(self, device_id: int) -> None:
        pass

    def get_version(self, device_id: int) -> tuple[str | None, str | None, str | None]:
        return "fake-1.0", "fake-1.0", "04.04"

    def connect(self, device_id: int, protocol: int, flags: int, bitrate: int) -> int:
        wants_29bit = bool(flags & CAN_29BIT_ID)
        vehicle_29bit = self._vehicle.profile.addressing == "29-bit"
        if wants_29bit != vehicle_29bit or bitrate != self._vehicle.profile.bitrate:
            raise ConnectError(
                f"FakeJ2534Lib: profile mismatch "
                f"(29bit req={wants_29bit} vehicle={vehicle_29bit}, "
                f"bitrate req={bitrate} vehicle={self._vehicle.profile.bitrate})"
            )
        self._rx_queue.clear()
        return 1  # fake channel_id

    def disconnect(self, channel_id: int) -> None:
        self._rx_queue.clear()

    def ioctl_teardown(self, channel_id: int) -> None:
        pass

    def read_one(self, channel_id: int, timeout_ms: int) -> tuple[int, bytes] | None:
        if self._rx_queue:
            return self._rx_queue.pop(0)
        if self._noop_delay > 0:
            time.sleep(min(self._noop_delay, timeout_ms / 1000.0))
        return None

    def write_msg(
        self, channel_id: int, data: bytes, tx_flags: int, timeout_ms: int
    ) -> None:
        from backprobe.transport.virtual import _respond  # lazy import (Decision 1)

        if len(data) < 4:
            return
        # TX echo (channel will skip via TX_MSG_TYPE bit)
        self._rx_queue.append((TX_MSG_TYPE, data))
        request = data[4:]
        for ecu in self._vehicle.ecus:
            payload = _respond(ecu, request)
            if payload is None:
                continue
            ecu_id_bytes = ecu.address.to_bytes(4, "big")
            if len(payload) > 7:
                # First-frame marker for multi-frame responses (channel skips this)
                self._rx_queue.append((START_OF_MESSAGE, ecu_id_bytes))
            self._rx_queue.append((0, ecu_id_bytes + payload))

    def start_filter(
        self,
        channel_id: int,
        filter_type: int,
        mask: bytes,
        pattern: bytes,
        flowctrl: bytes | None,
        *,
        tx_flags: int = 0,
    ) -> int:
        return 1  # fake filter_id

    def ioctl_set_config(self, channel_id: int, params: dict[int, int]) -> None:
        pass

    def ioctl_clear_rx(self, channel_id: int) -> str | None:
        self._rx_queue.clear()
        return None  # supported

    def ioctl_read_vbatt(self, device_id: int) -> int:
        return self._vbatt_mv


# ─── registry enumeration (Windows only) ────────────────────────────────────


def _enumerate_registry() -> list[Device]:
    """Scan HKLM + HKCU for J2534 04.04 DLLs. Returns [] on non-Windows or if
    nothing is installed. Never raises — dead/missing DLL entries are skipped."""
    if os.name != "nt":
        return []
    try:
        import winreg  # type: ignore[import]
    except ImportError:
        return []

    _log = session_log.get_logger("J2534")
    seen: set[str] = set()
    devices: list[Device] = []

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for reg_path in (
            REGISTRY_PATH,
            r"SOFTWARE\WOW6432Node\PassThruSupport.04.04",
        ):
            try:
                with winreg.OpenKey(hive, reg_path) as base:
                    i = 0
                    while True:
                        try:
                            entry_name = winreg.EnumKey(base, i)
                            with winreg.OpenKey(base, entry_name) as sub:
                                try:
                                    dll, _ = winreg.QueryValueEx(
                                        sub, REGISTRY_KEY_LIBRARY
                                    )
                                    dll_lower = dll.lower()
                                    if dll_lower in seen:
                                        i += 1
                                        continue
                                    seen.add(dll_lower)
                                    try:
                                        vendor, _ = winreg.QueryValueEx(
                                            sub, REGISTRY_KEY_VENDOR
                                        )
                                    except FileNotFoundError:
                                        vendor = entry_name
                                    try:
                                        name, _ = winreg.QueryValueEx(
                                            sub, REGISTRY_KEY_NAME
                                        )
                                    except FileNotFoundError:
                                        name = entry_name
                                    devices.append(
                                        Device(
                                            vendor=vendor, name=name, address=dll
                                        )
                                    )
                                except FileNotFoundError:
                                    pass
                            i += 1
                        except OSError:
                            break
            except (FileNotFoundError, PermissionError):
                pass

    return devices


# ─── backend ────────────────────────────────────────────────────────────────


class J2534Backend(TransportBackend):
    """The real hardware backend. enumerate() reads the Windows registry;
    open() loads the DLL and opens the device.

    lib= injection (Decision 1): pass a FakeJ2534Lib or a _RealJ2534Lib
    instance directly to bypass registry / DLL loading — used in tests and
    for future multi-device scenarios.
    """

    def __init__(self, *, lib: _J2534Lib | None = None) -> None:
        self._lib_override = lib
        self._log = session_log.get_logger("J2534")

    def enumerate(self) -> list[Device]:
        if self._lib_override is not None:
            return [Device(vendor="fake", name="FakeJ2534Device", address="fake://0")]
        return _enumerate_registry()

    def open(self, device: Device) -> "J2534Session":
        lib = self._lib_override
        if lib is None:
            try:
                lib = _RealJ2534Lib(device.address)
            except OSError as exc:
                raise DeviceLost(f"Cannot load DLL {device.address!r}: {exc}") from exc
        try:
            dev_id = lib.open(None)
        except (DeviceBusy, DeviceLost, InternalError):
            raise
        except Exception as exc:
            raise InternalError(f"PassThruOpen unexpected: {exc}") from exc
        fw, dll_v, api_v = lib.get_version(dev_id)
        report = DeviceReport(
            vendor=device.vendor,
            name=device.name,
            address=device.address,
            firmware=fw,
            dll_version=dll_v,
            api_version=api_v,
        )
        self._log.event("OPENED", device=device.name, dll=device.address,
                        firmware=fw, dll_version=dll_v)
        return J2534Session(lib=lib, device_id=dev_id, report=report)


# ─── session ────────────────────────────────────────────────────────────────


class J2534Session(Session):
    """One opened J2534 device, exclusively held."""

    def __init__(
        self, *, lib: _J2534Lib, device_id: int, report: DeviceReport
    ) -> None:
        self._lib     = lib
        self._dev_id  = device_id
        self._report  = report
        self._closed  = False
        self._log     = session_log.get_logger("J2534")

    def info(self) -> DeviceReport:
        return self._report

    def read_voltage(self) -> int:
        if self._closed:
            raise DeviceLost("session closed")
        return self._lib.ioctl_read_vbatt(self._dev_id)

    def connect(self, profile: ConnectProfile) -> "J2534Channel":
        if self._closed:
            raise DeviceLost("session closed")
        flags = CAN_29BIT_ID if profile.addressing == "29-bit" else 0
        ch_id = self._lib.connect(self._dev_id, ISO15765, flags, profile.bitrate)
        ch = J2534Channel(
            lib=self._lib,
            channel_id=ch_id,
            profile=profile,
            log=self._log,
        )
        ch._install_filters()
        ch._configure()
        return ch

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._lib.close(self._dev_id)


# ─── channel ────────────────────────────────────────────────────────────────


class J2534Channel(Channel):
    """A live ISO15765 connection to a vehicle ECU bus.

    The read loop (ask / ask_all):
      1. Clear RX buffer (Decision 3, primary). If unsupported, latch flag.
      2. Write functional request (0x7DF / 0x18DB33F1) with ISO15765_FRAME_PAD.
      3. Read, skipping TX echo (TX_MSG_TYPE) and first-frame markers (START_OF_MESSAGE).
      4. For ask: match expected SID + echoed PID as backstop (Decision 3, secondary).
      5. For ask_all: drain window, one reply per distinct ECU.
    """

    def __init__(
        self,
        *,
        lib: _J2534Lib,
        channel_id: int,
        profile: ConnectProfile,
        log: session_log.SessionLogger,
    ) -> None:
        self._lib  = lib
        self._ch_id = channel_id
        self._profile = profile
        self._log  = log
        self._disconnected = False
        self._clear_rx_tried = False   # have we tested CLEAR_RX_BUFFER yet?
        self._clear_rx_ok    = True    # assume supported until proven otherwise

        # Public flag — interrogate.probe() reads this to emit rx_clear_unsupported
        # to the event log (J2534Channel has no EventLog reference).
        self.rx_clear_unsupported: bool = False
        self._rx_clear_code: str = ""

    # ─── connect-time setup ───────────────────────────────────────────────

    def _install_filters(self) -> None:
        """Decision 2: one FLOW_CONTROL_FILTER per OBD ECU address.

        11-bit: patterns 0x7E8-0x7EF, flowctrl 0x7E0-0x7E7, mask 0x07FF.
        29-bit: ECU physical addresses 0x10-0x17 (Engine, TCM, ...),
                pattern 0x18DAF110-0x18DAF117, flowctrl 0x18DA10F1-0x18DA17F1,
                mask 0x1FFFFFFF.

        FC filter uses the PHYSICAL request ID (not functional 0x7DF / 0x18DB33F1)
        — OldVer's hard-won lesson: FC on the functional ID hangs every multi-frame
        reply forever (the ECU waits for a FC that goes to the wrong CAN ID).
        """
        if self._profile.addressing == "11-bit":
            mask_id = 0x07FF
            for i in range(8):
                self._lib.start_filter(
                    self._ch_id,
                    FLOW_CONTROL_FILTER,
                    mask=mask_id.to_bytes(4, "big"),
                    pattern=(0x7E8 + i).to_bytes(4, "big"),
                    flowctrl=(0x7E0 + i).to_bytes(4, "big"),
                )
        else:  # 29-bit
            mask_id = 0x1FFFFFFF
            for i in range(8):
                ecu_phys   = 0x10 + i
                pattern_id = 0x18DAF100 | ecu_phys
                fc_id      = 0x18DA0000 | (ecu_phys << 8) | 0xF1
                self._lib.start_filter(
                    self._ch_id,
                    FLOW_CONTROL_FILTER,
                    mask=mask_id.to_bytes(4, "big"),
                    pattern=pattern_id.to_bytes(4, "big"),
                    flowctrl=fc_id.to_bytes(4, "big"),
                    tx_flags=CAN_29BIT_ID,
                )

    def _configure(self) -> None:
        """Decision 6: LOOPBACK=0. BS/STMIN remain at DLL defaults."""
        self._lib.ioctl_set_config(self._ch_id, {LOOPBACK: 0})

    # ─── exchange helpers ─────────────────────────────────────────────────

    def _tx_id(self) -> int:
        return (
            OBD_FUNCTIONAL_29BIT
            if self._profile.addressing == "29-bit"
            else OBD_FUNCTIONAL_11BIT
        )

    def _tx_flags(self) -> int:
        flags = ISO15765_FRAME_PAD
        if self._profile.addressing == "29-bit":
            flags |= CAN_29BIT_ID
        return flags

    def _clear_rx(self) -> None:
        """Clear stale frames (Decision 3 primary). Latches RX_CLEAR_UNSUPPORTED once."""
        if self._clear_rx_tried and not self._clear_rx_ok:
            return  # already known to be unsupported; SID/PID match compensates
        err = self._lib.ioctl_clear_rx(self._ch_id)
        if err is not None:
            if not self._clear_rx_tried:
                self._clear_rx_ok  = False
                self.rx_clear_unsupported = True
                self._rx_clear_code = err
                self._log.event(
                    "RX_CLEAR_UNSUPPORTED",
                    code=err,
                    note="SID/PID match active as fallback",
                )
        self._clear_rx_tried = True

    def _write(self, request: bytes) -> None:
        tx_data = self._tx_id().to_bytes(4, "big") + request
        self._lib.write_msg(self._ch_id, tx_data, self._tx_flags(), 250)

    # ─── Channel interface ────────────────────────────────────────────────

    def ask(self, request: bytes, timeout: float) -> Reply:
        """Send one request, return the first matching reply.

        SID/PID match (Decision 3 backstop): expected SID = request_sid + 0x40;
        echoed PID must match. STALE_FRAME_DISCARDED logged per rejection.
        """
        if self._disconnected:
            raise DeviceLost("channel disconnected")
        self._clear_rx()
        self._write(request)

        expect_sid = (request[0] + 0x40) & 0xFF if request else None
        deadline_ms = max(1, int(timeout * 1000))
        start_ms    = _mono_ms()

        while True:
            elapsed = _mono_ms() - start_ms
            if elapsed >= deadline_ms:
                break
            msg = self._lib.read_one(self._ch_id, min(100, deadline_ms - elapsed))
            if msg is None:
                continue
            rx_status, data = msg
            if rx_status & TX_MSG_TYPE:
                continue
            if rx_status & START_OF_MESSAGE:
                continue
            if len(data) < 4:
                continue
            ecu_id  = int.from_bytes(data[:4], "big")
            payload = data[4:]
            if not payload:
                continue

            if expect_sid is not None:
                # Accept negative responses addressed to our service
                if (
                    payload[0] == 0x7F
                    and len(payload) >= 2
                    and payload[1] == request[0]
                ):
                    self._log.exchange(
                        self._tx_id(), request, [(ecu_id, payload)],
                        _mono_ms() - start_ms,
                    )
                    return Reply(ecu=ecu_id, payload=payload)
                if payload[0] != expect_sid:
                    self._log.event(
                        "STALE_FRAME_DISCARDED",
                        expected=f"{expect_sid:#04x}",
                        got=f"{payload[0]:#04x}",
                    )
                    continue
                if (
                    len(request) >= 2
                    and len(payload) >= 2
                    and payload[1] != request[1]
                ):
                    self._log.event(
                        "STALE_FRAME_DISCARDED",
                        expected_pid=f"{request[1]:#04x}",
                        got_pid=f"{payload[1]:#04x}",
                    )
                    continue

            self._log.exchange(
                self._tx_id(), request, [(ecu_id, payload)],
                _mono_ms() - start_ms,
            )
            return Reply(ecu=ecu_id, payload=payload)

        raise Timeout(f"no reply to {request.hex()} within {timeout}s")

    def ask_all(self, request: bytes, window: float) -> list[Reply]:
        """Send one functional request, collect every ECU answering in the window.

        No SID/PID filtering: the census window drains whatever is queued for
        the given window duration, grouping by ECU. [] is a valid result.
        """
        if self._disconnected:
            raise DeviceLost("channel disconnected")
        self._clear_rx()
        self._write(request)

        seen: dict[int, Reply] = {}
        deadline_ms    = max(1, int(window * 1000))
        start_ms       = _mono_ms()
        last_reply_ms: int | None = None

        while _mono_ms() - start_ms < deadline_ms:
            remaining = deadline_ms - (_mono_ms() - start_ms)
            if remaining <= 0:
                break
            if last_reply_ms is not None:
                coalesce_remaining = _COALESCE_MS - (_mono_ms() - last_reply_ms)
                if coalesce_remaining <= 0:
                    break
                remaining = min(remaining, coalesce_remaining)
            msg = self._lib.read_one(self._ch_id, min(50, remaining))
            if msg is None:
                continue
            rx_status, data = msg
            if rx_status & TX_MSG_TYPE:
                continue
            if rx_status & START_OF_MESSAGE:
                continue
            if len(data) < 4:
                continue
            ecu_id  = int.from_bytes(data[:4], "big")
            payload = data[4:]
            if payload and ecu_id not in seen:
                seen[ecu_id] = Reply(ecu=ecu_id, payload=payload)
                last_reply_ms = _mono_ms()

        replies = list(seen.values())
        self._log.exchange(
            self._tx_id(), request,
            [(r.ecu, r.payload) for r in replies],
            _mono_ms() - start_ms,
        )
        return replies

    def disconnect(self) -> None:
        if not self._disconnected:
            self._disconnected = True
            self._lib.ioctl_teardown(self._ch_id)
            self._lib.disconnect(self._ch_id)

"""J2534 passthru transport using direct ctypes DLL calls.

python-can 4.x removed its J2534 interface, so we talk to the DLL directly —
the same approach used by KLineTransport.  This is actually the correct
professional implementation: the J2534 DLL handles ISO-TP (ISO15765) framing
natively, so we don't need python-can-isotp at all.

Supports any SAE J2534-1 compliant device:
  - Opus IVS Supergoose Plus  (SuperGoosePlus_0404_32.dll)
  - Autel Maxiflash VCMI      (VCMI432.DLL)
  - OBDLink SX / MX / EX

All J2534 DLLs on Windows are 32-bit (stdcall calling convention).
This transport must run under 32-bit Python.

J2534 ISO15765 channel flow:
  1. PassThruOpen  → device handle
  2. PassThruConnect(ISO15765, 500 kbps) → channel handle
  3. PassThruStartMsgFilter(FLOW_CONTROL_FILTER) → ISO-TP FC handled by DLL
  4. PassThruWriteMsgs([tx_id 4 B] + UDS payload) → DLL segments into CAN frames
  5. PassThruReadMsgs → DLL reassembles ISO-TP response → [rx_id 4 B] + UDS payload
"""

from __future__ import annotations

import asyncio
import ctypes
import platform
import struct
import threading
from typing import Any

from obd2_mcp.config import J2534_KNOWN_DEVICES, settings
from obd2_mcp.transport.base import BaseTransport, TransportStatus

# ── J2534 protocol IDs ────────────────────────────────────────────────────────
_CAN      = 5
_ISO15765 = 6   # CAN with ISO-TP handled by DLL

# ── J2534 filter types ────────────────────────────────────────────────────────
_PASS_FILTER          = 0x01
_BLOCK_FILTER         = 0x02
_FLOW_CONTROL_FILTER  = 0x03   # ISO-TP flow control — required for multi-frame UDS

# ── J2534 ioctl IDs ──────────────────────────────────────────────────────────
_GET_CONFIG = 0x01
_SET_CONFIG = 0x02

# ── J2534 config param IDs ────────────────────────────────────────────────────
_DATA_RATE = 0x01
_LOOPBACK  = 0x03
_ISO15765_BS    = 0x16   # block size
_ISO15765_STMIN = 0x17   # separation time min

# ── TxFlags ──────────────────────────────────────────────────────────────────
_ISO15765_FRAME_PAD = 0x00000040
_CAN_29BIT_ID       = 0x00000100

# ── RxStatus bits ─────────────────────────────────────────────────────────────
_RX_TX_MSG_TYPE       = 0x01   # loopback / TX echo frame
_RX_START_OF_MESSAGE  = 0x02   # ISO15765 first-frame indication (empty payload)


# ── J2534 data structures ─────────────────────────────────────────────────────

class _SCONFIG(ctypes.Structure):
    _fields_ = [("Parameter", ctypes.c_ulong), ("Value", ctypes.c_ulong)]


class _SCONFIG_LIST(ctypes.Structure):
    _fields_ = [
        ("NumOfParams", ctypes.c_ulong),
        ("ConfigPtr",   ctypes.POINTER(_SCONFIG)),
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


# ── Registry helpers ──────────────────────────────────────────────────────────

def _find_j2534_dlls_from_registry() -> list[dict[str, str]]:
    """Scan the Windows registry for installed J2534 DLLs."""
    if platform.system() != "Windows":
        return []
    import winreg  # type: ignore[import]
    devices = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for reg_path in (
            r"SOFTWARE\PassThruSupport.04.04",
            r"SOFTWARE\WOW6432Node\PassThruSupport.04.04",
        ):
            try:
                with winreg.OpenKey(hive, reg_path) as base:
                    i = 0
                    while True:
                        try:
                            name = winreg.EnumKey(base, i)
                            with winreg.OpenKey(base, name) as sub:
                                try:
                                    dll, _ = winreg.QueryValueEx(sub, "FunctionLibrary")
                                    # Deduplicate by DLL path
                                    if not any(d["dll"].lower() == dll.lower() for d in devices):
                                        devices.append({"name": name, "dll": dll})
                                except FileNotFoundError:
                                    pass
                            i += 1
                        except OSError:
                            break
            except (FileNotFoundError, PermissionError):
                pass
    return devices


def _make_can_id_bytes(can_id: int) -> bytes:
    """Pack a CAN ID into 4 big-endian bytes (J2534 ISO15765 wire format)."""
    return struct.pack(">I", can_id)


def _make_filter_msg(protocol_id: int, can_id: int, pad: bool = False) -> _PASSTHRU_MSG:
    """Build a _PASSTHRU_MSG containing a 4-byte CAN ID for filter setup."""
    msg = _PASSTHRU_MSG()
    msg.ProtocolID = protocol_id
    msg.TxFlags    = _ISO15765_FRAME_PAD if pad else 0
    data           = _make_can_id_bytes(can_id)
    msg.DataSize   = len(data)
    for i, b in enumerate(data):
        msg.Data[i] = b
    return msg


# ── Transport ─────────────────────────────────────────────────────────────────

class J2534Transport(BaseTransport):
    """SAE J2534 passthru transport using direct ctypes DLL calls.

    Opens an ISO15765 channel so the DLL handles all ISO-TP framing.
    Works with Opus Supergoose Plus, Autel VCMI, OBDLink, and any other
    J2534-compliant interface installed on Windows.

    Requires 32-bit Python (all J2534 DLLs are 32-bit / stdcall).
    """

    def __init__(
        self,
        dll_path:    str | None = None,
        device_index: int = 0,
        can_fd:      bool | None = None,
        fd_data_rate: int | None = None,
    ) -> None:
        super().__init__()
        self._dll_path    = dll_path or settings.resolve_j2534_dll()
        self._device_index = device_index
        self._can_fd      = can_fd if can_fd is not None else settings.can_fd
        self._fd_data_rate = fd_data_rate or settings.can_fd_data_rate

        self._dll: Any = None
        self._device_id  = ctypes.c_ulong(0)
        self._channel_id = ctypes.c_ulong(0)
        # Serializes write+read on the single shared channel. Live-data polling
        # fires many PIDs concurrently via asyncio.gather (each in its own
        # executor thread); without this lock their requests/responses interleave
        # on the one channel and come back matched to the wrong request.
        self._io_lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("J2534Transport is only supported on Windows.")
        self._status = TransportStatus.CONNECTING
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._connect_sync)
            self._status = TransportStatus.CONNECTED
        except Exception as exc:
            self._status = TransportStatus.ERROR
            raise RuntimeError(f"J2534 connect failed: {exc}") from exc

    def _connect_sync(self) -> None:
        dll_path = self._dll_path
        if dll_path is None:
            found = _find_j2534_dlls_from_registry()
            if not found:
                raise RuntimeError(
                    "No J2534 DLL specified and none found in Windows registry. "
                    "Install your device driver (Opus IVS or Autel VCMI) first."
                )
            dll_path = found[self._device_index]["dll"]
            self._device_info["registry_name"] = found[self._device_index]["name"]

        # Load the DLL (stdcall — all J2534 DLLs use __stdcall on Windows)
        self._dll = ctypes.WinDLL(dll_path)  # type: ignore[attr-defined]
        self._dll_path = dll_path

        # PassThruOpen ────────────────────────────────────────────────────────
        dev_id = ctypes.c_ulong(0)
        ret = self._dll.PassThruOpen(None, ctypes.byref(dev_id))
        if ret != 0:
            raise RuntimeError(
                f"PassThruOpen failed (ret={ret:#010x}). "
                "Check that the device is plugged in and drivers are installed."
            )
        self._device_id = dev_id

        # PassThruConnect — ISO15765 at 500 kbps ──────────────────────────────
        ch_id  = ctypes.c_ulong(0)
        bitrate = 500_000
        ret = self._dll.PassThruConnect(
            dev_id, _ISO15765, 0x00, bitrate, ctypes.byref(ch_id)
        )
        if ret != 0:
            self._dll.PassThruClose(dev_id)
            raise RuntimeError(f"PassThruConnect(ISO15765) failed (ret={ret:#010x})")
        self._channel_id = ch_id

        # Disable loopback ────────────────────────────────────────────────────
        cfg    = (_SCONFIG * 1)()
        cfg[0] = _SCONFIG(Parameter=_LOOPBACK, Value=0)
        cfglist = _SCONFIG_LIST(NumOfParams=1, ConfigPtr=cfg)
        self._dll.PassThruIoctl(ch_id, _SET_CONFIG, ctypes.byref(cfglist), None)

        # FLOW_CONTROL_FILTER — required for multi-frame ISO-TP receive ─────────
        # When the ECM answers with a multi-frame message it sends a First Frame
        # and waits for a flow-control frame from us before sending the rest.
        # The DLL auto-sends that FC on the *flowctrl* CAN ID, which must be the
        # ECM's PHYSICAL request ID (0x7E0) — NOT the functional broadcast 0x7DF.
        # Sending FC on 0x7DF leaves the ECM waiting forever, so VIN (0902) and
        # Mode 06 monitors (0601) never complete and time out.
        #   mask:     exact-match 11-bit CAN ID
        #   pattern:  ECM response address (0x7E8)
        #   flowctrl: ECM physical request address (0x7E0)
        mask_msg     = _make_filter_msg(_ISO15765, 0x07FF, pad=True)
        pattern_msg  = _make_filter_msg(_ISO15765, 0x07E8, pad=True)
        flowctrl_msg = _make_filter_msg(_ISO15765, 0x07E0, pad=True)
        filter_id = ctypes.c_ulong(0)
        ret = self._dll.PassThruStartMsgFilter(
            ch_id, _FLOW_CONTROL_FILTER,
            ctypes.byref(mask_msg), ctypes.byref(pattern_msg),
            ctypes.byref(flowctrl_msg), ctypes.byref(filter_id),
        )
        # Non-fatal — some DLLs return warnings on filter setup. Record it so a
        # failed FC filter is visible when diagnosing multi-frame problems.
        self._device_info["fc_filter_ret"] = f"{ret:#010x}"

        self._device_info.update({
            "dll":      dll_path,
            "protocol": "ISO15765",
            "bitrate":  bitrate,
            "can_fd":   self._can_fd,
        })

    async def disconnect(self) -> None:
        if self._dll is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._disconnect_sync)
        self._status = TransportStatus.DISCONNECTED

    def _disconnect_sync(self) -> None:
        if self._dll is None:
            return
        try:
            self._dll.PassThruDisconnect(self._channel_id)
        except Exception:
            pass
        try:
            self._dll.PassThruClose(self._device_id)
        except Exception:
            pass
        self._dll = None

    # ── send/receive ──────────────────────────────────────────────────────────

    async def send_raw(
        self, data: bytes, tx_id: int, rx_id: int, timeout: float = 5.0
    ) -> bytes:
        """Send a UDS/OBD-II payload via ISO15765 and return the ECU response.

        The J2534 DLL handles all ISO-TP segmentation and reassembly.

        Args:
            data:    UDS payload bytes (service ID + params).
            tx_id:   CAN ID to transmit on (e.g. 0x7DF functional).
            rx_id:   Expected response CAN ID (e.g. 0x7E8 ECM physical).
            timeout: Seconds to wait for response.
        """
        if not self.is_connected or self._dll is None:
            raise RuntimeError("J2534 transport is not connected.")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._exchange_sync, data, tx_id, rx_id, timeout
        )

    def _exchange_sync(
        self, data: bytes, tx_id: int, rx_id: int, timeout: float
    ) -> bytes:
        """Synchronous ISO15765 send+receive.

        Held under ``self._io_lock`` for its full duration so concurrent pollers
        cannot interleave their write/read pairs on the shared channel.
        """
        # Expected positive-response service ID (e.g. 0x01 → 0x41, 0x09 → 0x49).
        # Used to discard stale/crossed frames left over from a prior request
        # that timed out on the bus but answered late.
        expect_sid = (data[0] + 0x40) & 0xFF if data else None

        with self._io_lock:
            # J2534 ISO15765 message format: [CAN_ID (4 bytes big-endian)] + payload
            tx_payload = _make_can_id_bytes(tx_id) + data

            tx = _PASSTHRU_MSG()
            tx.ProtocolID = _ISO15765
            tx.TxFlags    = _ISO15765_FRAME_PAD
            tx.DataSize   = len(tx_payload)
            for i, b in enumerate(tx_payload):
                tx.Data[i] = b

            timeout_ms = ctypes.c_ulong(int(timeout * 1000))
            num = ctypes.c_ulong(1)
            ret = self._dll.PassThruWriteMsgs(
                self._channel_id, ctypes.byref(tx), ctypes.byref(num), timeout_ms
            )
            if ret != 0:
                raise RuntimeError(f"PassThruWriteMsgs failed (ret={ret:#010x})")

            deadline_ms = int(timeout * 1000)
            while deadline_ms > 0:
                rx  = _PASSTHRU_MSG()
                num = ctypes.c_ulong(1)
                ret = self._dll.PassThruReadMsgs(
                    self._channel_id, ctypes.byref(rx), ctypes.byref(num),
                    ctypes.c_ulong(min(deadline_ms, 100)),
                )
                if ret == 0 and num.value > 0:
                    # Skip TX loopback echo frames.
                    if rx.RxStatus & _RX_TX_MSG_TYPE:
                        deadline_ms -= 100
                        continue
                    # Skip the ISO15765 first-frame indication: for a multi-frame
                    # response the DLL emits a START_OF_MESSAGE marker (empty
                    # payload) before the fully reassembled message. Returning it
                    # would yield an empty response (VIN 0902, monitors 0601).
                    if rx.RxStatus & _RX_START_OF_MESSAGE:
                        deadline_ms -= 100
                        continue
                    # Strip the 4-byte CAN ID prefix → raw UDS/OBD response.
                    raw = bytes(rx.Data[: rx.DataSize])[4:]
                    # Discard frames that don't answer this request (a late
                    # response to a previously timed-out PID still in the queue).
                    if expect_sid is not None and raw:
                        if raw[0] == 0x7F and len(raw) >= 2 and raw[1] == data[0]:
                            return raw  # negative response to our service
                        if raw[0] != expect_sid:
                            deadline_ms -= 100
                            continue
                        # Mode 01/02/06/09 echo the PID/MID in byte 2 — match it.
                        if len(data) >= 2 and len(raw) >= 2 and raw[1] != data[1]:
                            deadline_ms -= 100
                            continue
                    return raw
                deadline_ms -= 100

        raise TimeoutError(
            f"No ISO15765 response from 0x{rx_id:03X} within {timeout}s"
        )

    # ── device enumeration ────────────────────────────────────────────────────

    async def list_devices(self) -> list[dict[str, Any]]:
        """Return installed J2534 devices from the Windows registry."""
        registry_devices = _find_j2534_dlls_from_registry()
        for dev in registry_devices:
            for key, dll in J2534_KNOWN_DEVICES.items():
                if dll.lower() == dev["dll"].lower():
                    dev["known_as"] = key
                    break
        return registry_devices

    # ── raw bus (sniffer) ─────────────────────────────────────────────────────

    def get_raw_bus(self) -> Any:
        """J2534 ctypes transport does not expose a python-can Bus object.

        The CAN sniffer (CanSnifferProtocol) requires a python-can Bus.
        Use SocketCANTransport or VirtualTransport for passive CAN capture.
        Returns None — start_capture will raise a clear error.
        """
        return None

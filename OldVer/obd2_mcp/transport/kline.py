"""K-Line transport via J2534 passthru DLL (ctypes / stdcall).

Supports ISO 9141-2 and ISO 14230-4 (KWP2000) K-Line protocols used on:
  - Honda / Acura (~2000–2008, K-Line ECUs)
  - Toyota / Lexus pre-CAN
  - Many European vehicles pre-OBD-II CAN mandate

The J2534 DLL handles all physical-layer timing (P1–P4, W1–W5 intervals,
5-baud init or fast-init wakeup).  This module handles message framing,
checksum, and the async bridge.

Works with any J2534 device that supports ISO9141/ISO14230 protocols:
  - Opus IVS Supergoose Plus
  - Autel Maxiflash VCMI
  - OBDLink SX/MX (ISO14230 mode)
"""

from __future__ import annotations

import asyncio
import ctypes
import platform
from typing import Any

from obd2_mcp.config import settings
from obd2_mcp.transport.base import BaseTransport, TransportStatus

# ── J2534 protocol IDs ────────────────────────────────────────────────────────
_ISO9141  = 3
_ISO14230 = 4

# ── J2534 filter / ioctl constants ────────────────────────────────────────────
_PASS_FILTER = 0x01
_SET_CONFIG  = 0x02
_FAST_INIT   = 0x04
_LOOPBACK    = 0x03


# ── J2534 data structures ─────────────────────────────────────────────────────

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


# ── ISO 14230 framing helpers ─────────────────────────────────────────────────

def _iso14230_frame(target_addr: int, source_addr: int, data: bytes) -> bytes:
    """Wrap service bytes in an ISO 14230 header + checksum."""
    n = len(data)
    if n <= 0x3F:
        # Format byte encodes length directly
        header = bytes([0x80 | n, target_addr, source_addr])
    else:
        # Length byte separate (format byte length field = 0)
        header = bytes([0x80, target_addr, source_addr, n & 0xFF])
    payload = header + data
    return payload + bytes([sum(payload) & 0xFF])


def _iso14230_strip(raw: bytes) -> bytes:
    """Strip ISO 14230 header and trailing checksum; return service payload."""
    if len(raw) < 3:
        return raw
    fmt = raw[0]
    has_addr   = bool(fmt & 0x80)
    len_in_fmt = fmt & 0x3F

    if has_addr:
        if len_in_fmt == 0:
            hdr_len     = 4          # fmt + target + source + len_byte
            payload_len = raw[3]
        else:
            hdr_len     = 3          # fmt + target + source
            payload_len = len_in_fmt
    else:
        if len_in_fmt == 0:
            hdr_len     = 2
            payload_len = raw[1]
        else:
            hdr_len     = 1
            payload_len = len_in_fmt

    return raw[hdr_len : hdr_len + payload_len]


# ── Transport ─────────────────────────────────────────────────────────────────

class KLineTransport(BaseTransport):
    """K-Line diagnostic transport using a J2534 DLL (Windows, stdcall).

    Calls the DLL directly via ctypes so that ISO9141 and ISO14230
    channels — which python-can does not expose — are available.

    Usage::

        transport = KLineTransport(protocol="iso14230")
        await transport.connect()          # loads DLL, opens channel, fast-inits
        response = await transport.send_raw(bytes([0x01, 0x0C]), 0x33, 0xF1)
        # → RPM PID response bytes
    """

    def __init__(
        self,
        dll_path: str | None = None,
        protocol: str | None = None,
        target_addr: int | None = None,
        source_addr: int | None = None,
        device_index: int = 0,
    ) -> None:
        super().__init__()
        self._dll_path    = dll_path or settings.resolve_j2534_dll()
        proto             = (protocol or settings.kline_protocol).lower()
        self._protocol_id = _ISO14230 if proto == "iso14230" else _ISO9141
        self._proto_name  = proto
        self._target_addr = target_addr if target_addr is not None else settings.kline_target_addr
        self._source_addr = source_addr if source_addr is not None else settings.kline_source_addr
        self._device_index = device_index

        self._dll: Any = None          # ctypes.WinDLL on Windows
        self._device_id  = ctypes.c_ulong(0)
        self._channel_id = ctypes.c_ulong(0)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if platform.system() != "Windows":
            raise RuntimeError(
                "KLineTransport requires a J2534 DLL and is only supported on Windows."
            )
        self._status = TransportStatus.CONNECTING
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._connect_sync)
            self._status = TransportStatus.CONNECTED
            self._device_info = {
                "transport":   "kline",
                "protocol":    self._proto_name,
                "dll":         self._dll_path,
                "target_addr": hex(self._target_addr),
                "source_addr": hex(self._source_addr),
            }
        except Exception as exc:
            self._status = TransportStatus.ERROR
            raise RuntimeError(f"K-Line connect failed: {exc}") from exc

    def _connect_sync(self) -> None:
        dll_path = self._dll_path
        if dll_path is None:
            from obd2_mcp.transport.j2534 import _find_j2534_dlls_from_registry
            found = _find_j2534_dlls_from_registry()
            if not found:
                raise RuntimeError("No J2534 DLL found in Windows registry.")
            dll_path = found[self._device_index]["dll"]
            self._device_info["registry_name"] = found[self._device_index]["name"]

        # Load DLL (stdcall calling convention)
        self._dll = ctypes.WinDLL(dll_path)  # type: ignore[attr-defined]
        self._dll_path = dll_path

        # PassThruOpen ────────────────────────────────────────────────────────
        dev_id = ctypes.c_ulong(0)
        ret = self._dll.PassThruOpen(None, ctypes.byref(dev_id))
        if ret != 0:
            raise RuntimeError(f"PassThruOpen failed: {ret:#010x}")
        self._device_id = dev_id

        # PassThruConnect at 10400 baud ───────────────────────────────────────
        ch_id = ctypes.c_ulong(0)
        ret = self._dll.PassThruConnect(
            dev_id, self._protocol_id, 0x00, 10400, ctypes.byref(ch_id)
        )
        if ret != 0:
            self._dll.PassThruClose(dev_id)
            raise RuntimeError(f"PassThruConnect failed: {ret:#010x}")
        self._channel_id = ch_id

        # Pass-all filter (mask = 0 bytes) ────────────────────────────────────
        mask    = _PASSTHRU_MSG(); mask.ProtocolID    = self._protocol_id; mask.DataSize    = 0
        pattern = _PASSTHRU_MSG(); pattern.ProtocolID = self._protocol_id; pattern.DataSize = 0
        fid = ctypes.c_ulong(0)
        self._dll.PassThruStartMsgFilter(
            ch_id, _PASS_FILTER,
            ctypes.byref(mask), ctypes.byref(pattern),
            None, ctypes.byref(fid),
        )

        # Disable loopback ────────────────────────────────────────────────────
        cfg    = (_SCONFIG * 1)()
        cfg[0] = _SCONFIG(Parameter=_LOOPBACK, Value=0)
        cfglist = _SCONFIG_LIST(NumOfParams=1, ConfigPtr=cfg)
        self._dll.PassThruIoctl(ch_id, _SET_CONFIG, ctypes.byref(cfglist), None)

        # Fast init (ISO14230 only) ────────────────────────────────────────────
        if self._proto_name == "iso14230":
            self._fast_init_sync()

    def _fast_init_sync(self) -> None:
        """ISO 14230 fast-init (FAST_INIT ioctl): sends wakeup pattern."""
        # StartCommunication request bytes (no checksum — DLL adds it)
        init_bytes = bytes([0xC1, self._target_addr, self._source_addr])
        init_msg = _PASSTHRU_MSG()
        init_msg.ProtocolID = _ISO14230
        init_msg.TxFlags    = 0x00
        init_msg.DataSize   = len(init_bytes)
        for i, b in enumerate(init_bytes):
            init_msg.Data[i] = b

        resp_msg = _PASSTHRU_MSG()
        # Non-zero return is non-fatal: some DLLs return ERR_NOT_SUPPORTED
        # but the channel is still usable after connect
        self._dll.PassThruIoctl(
            self._channel_id, _FAST_INIT,
            ctypes.byref(init_msg), ctypes.byref(resp_msg),
        )

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
        """Send a KWP2000 / OBD-II request and return the response payload.

        Args:
            data:    Service bytes (service ID + params), no header or checksum.
            tx_id:   Target K-Line address (0x33 = OBD-II functional broadcast).
            rx_id:   Tester source address (0xF1 = standard external tester).
            timeout: Seconds to wait for ECU response.

        Returns:
            Response payload bytes (service ID + data), header and checksum stripped.
        """
        if not self.is_connected or self._dll is None:
            raise RuntimeError("K-Line transport is not connected.")

        target = tx_id or self._target_addr
        source = rx_id or self._source_addr
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._exchange_sync, data, target, source, timeout
        )

    def _exchange_sync(
        self, data: bytes, target: int, source: int, timeout: float
    ) -> bytes:
        framed  = _iso14230_frame(target, source, data)
        timeout_ms = ctypes.c_ulong(int(timeout * 1000))

        tx = _PASSTHRU_MSG()
        tx.ProtocolID = self._protocol_id
        tx.TxFlags    = 0x00
        tx.DataSize   = len(framed)
        for i, b in enumerate(framed):
            tx.Data[i] = b

        num = ctypes.c_ulong(1)
        ret = self._dll.PassThruWriteMsgs(
            self._channel_id, ctypes.byref(tx), ctypes.byref(num), timeout_ms
        )
        if ret != 0:
            raise RuntimeError(f"PassThruWriteMsgs failed: {ret:#010x}")

        rx  = _PASSTHRU_MSG()
        num = ctypes.c_ulong(1)
        ret = self._dll.PassThruReadMsgs(
            self._channel_id, ctypes.byref(rx), ctypes.byref(num), timeout_ms
        )
        if ret != 0:
            raise TimeoutError(
                f"PassThruReadMsgs timed out (ret={ret:#010x}) — no response within {timeout}s"
            )

        raw = bytes(rx.Data[: rx.DataSize])
        return _iso14230_strip(raw)

    async def list_devices(self) -> list[dict[str, Any]]:
        from obd2_mcp.transport.j2534 import _find_j2534_dlls_from_registry
        devs = _find_j2534_dlls_from_registry()
        return [{"name": d["name"], "dll": d["dll"], "protocol": "kline"} for d in devs]

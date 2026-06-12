"""OBD2 AI Tool — MCP Server entry point.

Exposes vehicle diagnostic capabilities as MCP tools that Claude can call.

Run with:
    obd2-mcp                          # uses config / .env
    OBD2_TRANSPORT=virtual obd2-mcp   # offline simulation
    OBD2_TRANSPORT=j2534 OBD2_J2534_DLL=opus_supergoose obd2-mcp

Configure Claude Desktop (claude_desktop_config.json):
    {
      "mcpServers": {
        "obd2": {
          "command": "obd2-mcp",
          "env": {
            "OBD2_TRANSPORT": "j2534",
            "OBD2_J2534_DLL": "opus_supergoose"
          }
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from obd2_mcp.config import TransportType, settings
from obd2_mcp.instance_lock import acquire_instance_lock
from obd2_mcp.protocols.kwp2000 import KWP2000Protocol
from obd2_mcp.protocols.obd2 import OBD2Protocol
from obd2_mcp.protocols.sniffer import CanSnifferProtocol
from obd2_mcp.protocols.uds import UDSProtocol
from obd2_mcp.session_log import (
    install_crash_handler,
    log_error,
    log_event,
    log_tool_call,
    log_tool_result,
    log_system_info,
    set_source,
)
from obd2_mcp.transport.base import BaseTransport

logger = logging.getLogger("obd2_mcp")


# ---------------------------------------------------------------------------
# Transport factory
# ---------------------------------------------------------------------------

def _build_transport() -> BaseTransport:
    match settings.transport:
        case TransportType.J2534:
            from obd2_mcp.transport.j2534 import J2534Transport
            return J2534Transport(
                dll_path=settings.resolve_j2534_dll(),
                device_index=settings.j2534_device_index,
            )
        case TransportType.ELM327:
            from obd2_mcp.transport.elm327 import ELM327Transport
            return ELM327Transport(
                port=settings.elm327_port,
                baudrate=settings.elm327_baudrate,
                fast=settings.elm327_fast,
                timeout=settings.elm327_timeout,
            )
        case TransportType.SOCKETCAN:
            from obd2_mcp.transport.socketcan import SocketCANTransport
            return SocketCANTransport(
                channel=settings.socketcan_channel,
                bitrate=settings.socketcan_bitrate,
            )
        case TransportType.KLINE:
            from obd2_mcp.transport.kline import KLineTransport
            return KLineTransport(
                dll_path=settings.resolve_j2534_dll(),
                protocol=settings.kline_protocol,
                target_addr=settings.kline_target_addr,
                source_addr=settings.kline_source_addr,
            )
        case TransportType.DOIP:
            from obd2_mcp.transport.doip import DoIPTransport
            return DoIPTransport(
                host=settings.doip_host,
                port=settings.doip_port,
                source_addr=settings.doip_source_addr,
                target_addr=settings.doip_target_addr,
            )
        case TransportType.VIRTUAL:
            from obd2_mcp.transport.virtual import VirtualTransport
            return VirtualTransport()
        case _:
            raise ValueError(f"Unknown transport: {settings.transport}")


# ---------------------------------------------------------------------------
# Server lifecycle context
# ---------------------------------------------------------------------------

class _AppContext:
    def __init__(self) -> None:
        self.transport: BaseTransport | None = None
        self.obd2: OBD2Protocol | None = None
        self.uds: UDSProtocol | None = None
        self.kwp2000: KWP2000Protocol | None = None
        self.sniffer: CanSnifferProtocol | None = None


_ctx = _AppContext()


@asynccontextmanager
async def lifespan(server: Server) -> AsyncIterator[None]:
    from obd2_mcp.session_log import instrument_transport
    transport = _build_transport()
    instrument_transport(transport)
    _ctx.transport = transport
    _ctx.obd2 = OBD2Protocol(transport)
    _ctx.uds = UDSProtocol(transport)
    _ctx.kwp2000 = KWP2000Protocol(transport)
    _ctx.sniffer = CanSnifferProtocol(transport)
    try:
        yield
    finally:
        if transport.is_connected:
            await transport.disconnect()


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server(settings.mcp_server_name)


def _require_connection() -> tuple[OBD2Protocol, UDSProtocol]:
    if _ctx.transport is None or not _ctx.transport.is_connected:
        raise RuntimeError(
            "Not connected to a vehicle. Call 'connect_device' first."
        )
    assert _ctx.obd2 and _ctx.uds
    return _ctx.obd2, _ctx.uds


def _require_kwp2000() -> KWP2000Protocol:
    if _ctx.transport is None or not _ctx.transport.is_connected:
        raise RuntimeError(
            "Not connected to a vehicle. Call 'connect_device' first."
        )
    assert _ctx.kwp2000
    return _ctx.kwp2000


def _require_sniffer() -> CanSnifferProtocol:
    if _ctx.transport is None or not _ctx.transport.is_connected:
        raise RuntimeError(
            "Not connected to a vehicle. Call 'connect_device' first."
        )
    assert _ctx.sniffer
    return _ctx.sniffer


# ------------------------------------------------------------------
# Tool: list_devices
# ------------------------------------------------------------------

@server.tool(
    name="list_devices",
    description=(
        "List available OBD2 diagnostic devices. "
        "For J2534 transport this scans the Windows registry for installed passthru DLLs "
        "(Opus IVS Supergoose Plus, Autel Maxiflash VCMI, etc.). "
        "For ELM327 it lists available serial ports. "
        "Call this before connect_device to see what's available."
    ),
)
async def list_devices() -> list[types.TextContent]:
    log_tool_call("list_devices", {})
    if _ctx.transport is None:
        transport = _build_transport()
        _ctx.transport = transport

    devices = await _ctx.transport.list_devices()
    log_tool_result("list_devices", True, f"found={len(devices)}")
    import json
    return [types.TextContent(type="text", text=json.dumps(devices, indent=2))]


# ------------------------------------------------------------------
# Tool: connect_device
# ------------------------------------------------------------------

@server.tool(
    name="connect_device",
    description=(
        "Connect to the vehicle via the configured transport (J2534, ELM327, or SocketCAN). "
        "Must be called before any diagnostic commands. "
        "Returns device info and connection status."
    ),
)
async def connect_device() -> list[types.TextContent]:
    log_tool_call("connect_device", {})
    if _ctx.transport is None:
        _ctx.transport = _build_transport()
        _ctx.obd2 = OBD2Protocol(_ctx.transport)
        _ctx.uds = UDSProtocol(_ctx.transport)

    if _ctx.transport.is_connected:
        log_tool_result("connect_device", True, "already connected")
        return [types.TextContent(type="text", text="Already connected.")]

    await _ctx.transport.connect()
    log_tool_result("connect_device", True,
                    f"device_info={_ctx.transport.device_info!r}")
    import json
    return [types.TextContent(
        type="text",
        text=json.dumps({
            "status": _ctx.transport.status.value,
            "device_info": _ctx.transport.device_info,
        }, indent=2),
    )]


# ------------------------------------------------------------------
# Tool: disconnect_device
# ------------------------------------------------------------------

@server.tool(
    name="disconnect_device",
    description="Disconnect from the vehicle and release the hardware interface.",
)
async def disconnect_device() -> list[types.TextContent]:
    log_tool_call("disconnect_device", {})
    if _ctx.transport and _ctx.transport.is_connected:
        await _ctx.transport.disconnect()
        log_tool_result("disconnect_device", True)
        return [types.TextContent(type="text", text="Disconnected.")]
    log_tool_result("disconnect_device", True, "was not connected")
    return [types.TextContent(type="text", text="Not connected.")]


# ------------------------------------------------------------------
# Tool: read_pids
# ------------------------------------------------------------------

@server.tool(
    name="read_pids",
    description=(
        "Query one or more OBD-II Mode 01 PIDs (real-time sensor data). "
        "Common PIDs: 0x0C=RPM, 0x0D=Speed, 0x05=Coolant Temp, 0x04=Engine Load, "
        "0x11=Throttle Position, 0x0B=MAP Pressure, 0x0F=Intake Air Temp, 0x1F=Run Time. "
        "Returns values with units where available."
    ),
)
async def read_pids(
    pids: list[int] | None = None,
    mode: int = 1,
) -> list[types.TextContent]:
    """
    Args:
        pids: List of PID integers to query. Omit for a standard live-data snapshot.
        mode: OBD-II mode (default 1 = current data, 2 = freeze frame).
    """
    log_tool_call("read_pids", {"pids": pids, "mode": mode})
    obd2, _ = _require_connection()
    if pids:
        tasks = [obd2.query_pid(p, mode) for p in pids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        data = [r if isinstance(r, dict) else {"error": str(r)} for r in results]
    else:
        data = await obd2.live_data_snapshot()

    import json
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


# ------------------------------------------------------------------
# Tool: read_dtcs
# ------------------------------------------------------------------

@server.tool(
    name="read_dtcs",
    description=(
        "Read stored Diagnostic Trouble Codes (DTCs) from the vehicle. "
        "Returns a list of codes like ['P0300', 'P0420']. "
        "Use read_uds_dtcs for extended UDS DTC reading with status information."
    ),
)
async def read_dtcs() -> list[types.TextContent]:
    log_tool_call("read_dtcs", {})
    obd2, _ = _require_connection()
    codes = await obd2.read_dtcs()
    log_tool_result("read_dtcs", True, f"count={len(codes)} codes={codes}")
    import json
    return [types.TextContent(
        type="text",
        text=json.dumps({"dtcs": codes, "count": len(codes)}, indent=2),
    )]


# ------------------------------------------------------------------
# Tool: clear_dtcs
# ------------------------------------------------------------------

@server.tool(
    name="clear_dtcs",
    description=(
        "Clear all stored DTCs and reset the MIL (check engine light). "
        "WARNING: This erases freeze frame data and resets readiness monitors. "
        "Confirm with the user before calling this."
    ),
)
async def clear_dtcs() -> list[types.TextContent]:
    log_tool_call("clear_dtcs", {})
    obd2, _ = _require_connection()
    success = await obd2.clear_dtcs()
    log_tool_result("clear_dtcs", success)
    return [types.TextContent(
        type="text",
        text="DTCs cleared successfully." if success else "Failed to clear DTCs.",
    )]


# ------------------------------------------------------------------
# Tool: live_data_snapshot
# ------------------------------------------------------------------

@server.tool(
    name="live_data_snapshot",
    description=(
        "Take a snapshot of all key live sensor readings at once. "
        "Returns RPM, vehicle speed, coolant temp, engine load, throttle position, "
        "MAP pressure, intake air temp, and engine run time concurrently."
    ),
)
async def live_data_snapshot() -> list[types.TextContent]:
    log_tool_call("live_data_snapshot", {})
    obd2, _ = _require_connection()
    data = await obd2.live_data_snapshot()
    log_tool_result("live_data_snapshot", True, f"pids={len(data)}")
    import json
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


# ------------------------------------------------------------------
# Tool: read_vin
# ------------------------------------------------------------------

@server.tool(
    name="read_vin",
    description="Read the Vehicle Identification Number (VIN) via UDS DID 0xF190.",
)
async def read_vin() -> list[types.TextContent]:
    log_tool_call("read_vin", {})
    _, uds = _require_connection()
    result = await uds.read_data_by_id(0xF190)
    log_tool_result("read_vin", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: read_did
# ------------------------------------------------------------------

@server.tool(
    name="read_did",
    description=(
        "Read a UDS Data Identifier (DID) value from an ECU using Service 0x22. "
        "Common DIDs: 0xF190=VIN, 0xF18C=ECU Serial, 0xF187=Part Number, 0xF80A=Cal ID. "
        "Returns raw hex and parsed value where available."
    ),
)
async def read_did(did: int) -> list[types.TextContent]:
    """
    Args:
        did: The DID as an integer (e.g. 0xF190 = 61840).
    """
    log_tool_call("read_did", {"did": hex(did)})
    _, uds = _require_connection()
    result = await uds.read_data_by_id(did)
    log_tool_result("read_did", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: read_uds_dtcs
# ------------------------------------------------------------------

@server.tool(
    name="read_uds_dtcs",
    description=(
        "Read DTCs using UDS Service 0x19 ReadDTCInformation. "
        "Returns DTC IDs with status bytes (confirmed, pending, permanent, etc.). "
        "More detailed than the basic OBD-II Mode 03 read_dtcs tool. "
        "Requires an extended diagnostic session — call change_session first if needed."
    ),
)
async def read_uds_dtcs(status_mask: int = 0xFF) -> list[types.TextContent]:
    """
    Args:
        status_mask: DTC status mask byte (0xFF = all statuses).
    """
    log_tool_call("read_uds_dtcs", {"status_mask": hex(status_mask)})
    _, uds = _require_connection()
    dtcs = await uds.read_uds_dtcs(status_mask=status_mask)
    log_tool_result("read_uds_dtcs", True, f"count={len(dtcs)} dtcs={dtcs}")
    import json
    return [types.TextContent(
        type="text",
        text=json.dumps({"dtcs": dtcs, "count": len(dtcs)}, indent=2),
    )]


# ------------------------------------------------------------------
# Tool: clear_uds_dtcs
# ------------------------------------------------------------------

@server.tool(
    name="clear_uds_dtcs",
    description=(
        "Clear DTCs via UDS Service 0x14 ClearDiagnosticInformation. "
        "group_of_dtc=0xFFFFFF clears all DTCs (default). "
        "WARNING: Confirm with the user before calling."
    ),
)
async def clear_uds_dtcs(group_of_dtc: int = 0xFFFFFF) -> list[types.TextContent]:
    log_tool_call("clear_uds_dtcs", {"group_of_dtc": hex(group_of_dtc)})
    _, uds = _require_connection()
    result = await uds.clear_uds_dtcs(group_of_dtc)
    log_tool_result("clear_uds_dtcs", result.get("success", False), f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: change_session
# ------------------------------------------------------------------

@server.tool(
    name="change_session",
    description=(
        "Change the UDS diagnostic session. "
        "1=default, 2=programming, 3=extended (required for most bidirectional tests). "
        "Must call this before routines, security access, or write operations."
    ),
)
async def change_session(session: int = 3) -> list[types.TextContent]:
    log_tool_call("change_session", {"session": session})
    _, uds = _require_connection()
    result = await uds.change_session(session)
    log_tool_result("change_session", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: ecu_reset
# ------------------------------------------------------------------

@server.tool(
    name="ecu_reset",
    description=(
        "Send a UDS ECU reset command. "
        "reset_type: 1=hardReset, 2=keyOffOnReset, 3=softReset. "
        "WARNING: This will reset the ECU. Confirm with the user first."
    ),
)
async def ecu_reset(reset_type: int = 1) -> list[types.TextContent]:
    log_tool_call("ecu_reset", {"reset_type": reset_type})
    _, uds = _require_connection()
    result = await uds.ecu_reset(reset_type)
    log_tool_result("ecu_reset", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: start_routine
# ------------------------------------------------------------------

@server.tool(
    name="start_routine",
    description=(
        "Start a UDS diagnostic routine (Service 0x31 RoutineControl). "
        "routine_id and data are OEM-specific. "
        "Examples: 0xFF00=Check Programming Dependencies, 0x0202=Actuator Test. "
        "Returns the ECU's response payload."
    ),
)
async def start_routine(
    routine_id: int,
    data_hex: str = "",
) -> list[types.TextContent]:
    """
    Args:
        routine_id: Routine identifier integer.
        data_hex: Optional hex string of additional routine data (e.g. '0102AB').
    """
    log_tool_call("start_routine", {"routine_id": hex(routine_id), "data_hex": data_hex})
    _, uds = _require_connection()
    data = bytes.fromhex(data_hex) if data_hex else b""
    result = await uds.start_routine(routine_id, data)
    log_tool_result("start_routine", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: stop_routine
# ------------------------------------------------------------------

@server.tool(
    name="stop_routine",
    description="Stop a running UDS diagnostic routine (Service 0x31 subfunction 0x02).",
)
async def stop_routine(routine_id: int) -> list[types.TextContent]:
    log_tool_call("stop_routine", {"routine_id": hex(routine_id)})
    _, uds = _require_connection()
    result = await uds.stop_routine(routine_id)
    log_tool_result("stop_routine", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: get_routine_result
# ------------------------------------------------------------------

@server.tool(
    name="get_routine_result",
    description="Request the result of a previously started UDS routine (0x31 subfunction 0x03).",
)
async def get_routine_result(routine_id: int) -> list[types.TextContent]:
    log_tool_call("get_routine_result", {"routine_id": hex(routine_id)})
    _, uds = _require_connection()
    result = await uds.get_routine_result(routine_id)
    log_tool_result("get_routine_result", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: kwp_start_session
# ------------------------------------------------------------------

@server.tool(
    name="kwp_start_session",
    description=(
        "Start a KWP2000 diagnostic session (K-Line vehicles — Honda, Toyota pre-CAN, etc.). "
        "mode: 0x89=standard, 0x85=extended, 0x82=Honda dealer session. "
        "Call this before KWP2000 read/clear operations on K-Line ECUs."
    ),
)
async def kwp_start_session(mode: int = 0x89) -> list[types.TextContent]:
    log_tool_call("kwp_start_session", {"mode": hex(mode)})
    kwp = _require_kwp2000()
    result = await kwp.start_session(mode)
    log_tool_result("kwp_start_session", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: read_kwp_dtcs
# ------------------------------------------------------------------

@server.tool(
    name="read_kwp_dtcs",
    description=(
        "Read DTCs from a K-Line ECU using KWP2000 service 0x13. "
        "Returns DTC codes with status bytes (active, confirmed, flags). "
        "Use this instead of read_dtcs for Honda / pre-CAN vehicles on K-Line transport."
    ),
)
async def read_kwp_dtcs(status_group: int = 0xFF) -> list[types.TextContent]:
    log_tool_call("read_kwp_dtcs", {"status_group": hex(status_group)})
    kwp = _require_kwp2000()
    dtcs = await kwp.read_dtcs(status_group)
    log_tool_result("read_kwp_dtcs", True, f"count={len(dtcs)} dtcs={dtcs}")
    import json
    return [types.TextContent(
        type="text",
        text=json.dumps({"dtcs": dtcs, "count": len(dtcs)}, indent=2),
    )]


# ------------------------------------------------------------------
# Tool: clear_kwp_dtcs
# ------------------------------------------------------------------

@server.tool(
    name="clear_kwp_dtcs",
    description=(
        "Clear DTCs on a K-Line ECU via KWP2000 service 0x14. "
        "WARNING: Erases all stored fault codes and resets readiness monitors. "
        "Confirm with the user before calling."
    ),
)
async def clear_kwp_dtcs() -> list[types.TextContent]:
    log_tool_call("clear_kwp_dtcs", {})
    kwp = _require_kwp2000()
    success = await kwp.clear_dtcs()
    log_tool_result("clear_kwp_dtcs", success)
    return [types.TextContent(
        type="text",
        text="KWP2000 DTCs cleared." if success else "KWP2000 clear returned failure.",
    )]


# ------------------------------------------------------------------
# Tool: read_kwp_local_id
# ------------------------------------------------------------------

@server.tool(
    name="read_kwp_local_id",
    description=(
        "Read a Honda K-Line Local Identifier (LID) via KWP2000 service 0x21. "
        "Known Honda LIDs: 0x11=engine block (RPM/speed/temps/battery), "
        "0x12=O2 sensors, 0x14=fuel trim. "
        "Returns structured data for known LIDs; raw hex for unknown ones."
    ),
)
async def read_kwp_local_id(local_id: int) -> list[types.TextContent]:
    """
    Args:
        local_id: Honda Local Identifier integer (e.g. 0x11 = 17).
    """
    log_tool_call("read_kwp_local_id", {"local_id": hex(local_id)})
    kwp = _require_kwp2000()
    result = await kwp.read_data_by_local_id(local_id)
    log_tool_result("read_kwp_local_id", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: read_kwp_common_id
# ------------------------------------------------------------------

@server.tool(
    name="read_kwp_common_id",
    description=(
        "Read a Data Identifier from a K-Line ECU via KWP2000 service 0x22. "
        "Same DID space as UDS: 0xF190=VIN, 0xF187=Part Number, 0xF80A=Cal ID. "
        "Use this on K-Line vehicles instead of read_did."
    ),
)
async def read_kwp_common_id(did: int) -> list[types.TextContent]:
    log_tool_call("read_kwp_common_id", {"did": hex(did)})
    kwp = _require_kwp2000()
    result = await kwp.read_data_by_common_id(did)
    log_tool_result("read_kwp_common_id", "error" not in result, f"result={result!r}")
    import json
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Tool: load_dbc
# ------------------------------------------------------------------

@server.tool(
    name="load_dbc",
    description=(
        "Load a DBC (or KCD/SYM) CAN signal database file. "
        "This tells the sniffer how to decode raw CAN frames from every module on the bus. "
        "Returns a summary of all messages and signals found in the file. "
        "Must be called before start_capture will produce decoded signal data. "
        "DBC files can be found in the opendbc project (comma.ai) or from OEM sources."
    ),
)
async def load_dbc(path: str) -> list[types.TextContent]:
    """
    Args:
        path: Absolute path to the .dbc, .kcd, or .sym file.
    """
    log_tool_call("load_dbc", {"path": path})
    sniffer = _require_sniffer()
    import json
    summary = sniffer.load_dbc(path)
    log_tool_result("load_dbc", True,
                    f"messages={summary.get('message_count')} signals={summary.get('signal_count')}")
    return [types.TextContent(type="text", text=json.dumps(summary, indent=2))]


# ------------------------------------------------------------------
# Tool: find_dbc_signals
# ------------------------------------------------------------------

@server.tool(
    name="find_dbc_signals",
    description=(
        "Search the loaded DBC file for signals matching a keyword. "
        "Useful for finding A/C, pressure, temperature, or other signals "
        "across all modules without knowing the exact names. "
        "Returns signal name, parent message, unit, min/max, and named values."
    ),
)
async def find_dbc_signals(keyword: str | None = None) -> list[types.TextContent]:
    """
    Args:
        keyword: Search term (case-insensitive). Omit to list all signals.
    """
    log_tool_call("find_dbc_signals", {"keyword": keyword})
    sniffer = _require_sniffer()
    import json
    results = sniffer.list_signals(keyword)
    log_tool_result("find_dbc_signals", True, f"matches={len(results)}")
    return [types.TextContent(
        type="text",
        text=json.dumps({"keyword": keyword, "count": len(results), "signals": results}, indent=2),
    )]


# ------------------------------------------------------------------
# Tool: start_capture
# ------------------------------------------------------------------

@server.tool(
    name="start_capture",
    description=(
        "Start passive CAN bus capture. "
        "Once running, every frame broadcast by every module on the bus is received "
        "and decoded simultaneously using the loaded DBC file -- "
        "no polling, no sequential querying. "
        "Call get_signal_snapshot or correlate_signals after capturing data. "
        "The capture runs in the background until stop_capture is called."
    ),
)
async def start_capture() -> list[types.TextContent]:
    log_tool_call("start_capture", {})
    sniffer = _require_sniffer()
    if sniffer.is_running:
        log_tool_result("start_capture", True, "already running")
        return [types.TextContent(type="text", text="Capture already running.")]
    await sniffer.start()
    log_tool_result("start_capture", True, f"dbc={sniffer.stats()['dbc_loaded']!r}")
    import json
    return [types.TextContent(
        type="text",
        text=json.dumps({"status": "capture started", "dbc_loaded": sniffer.stats()["dbc_loaded"]}),
    )]


# ------------------------------------------------------------------
# Tool: stop_capture
# ------------------------------------------------------------------

@server.tool(
    name="stop_capture",
    description="Stop the passive CAN bus capture and return final statistics.",
)
async def stop_capture() -> list[types.TextContent]:
    log_tool_call("stop_capture", {})
    sniffer = _require_sniffer()
    stats = sniffer.stats()
    await sniffer.stop()
    log_tool_result("stop_capture", True,
                    f"frames={stats.get('frames_received')} decoded={stats.get('frames_decoded')}"
                    f" unknown={stats.get('frames_unknown')}")
    import json
    return [types.TextContent(type="text", text=json.dumps(stats, indent=2))]


# ------------------------------------------------------------------
# Tool: capture_stats
# ------------------------------------------------------------------

@server.tool(
    name="capture_stats",
    description=(
        "Return live capture statistics: frames received/decoded/unknown, "
        "elapsed time, number of signals being tracked, and any unknown CAN IDs "
        "seen on the bus that are not in the DBC file."
    ),
)
async def capture_stats() -> list[types.TextContent]:
    log_tool_call("capture_stats", {})
    sniffer = _require_sniffer()
    import json
    stats = sniffer.stats()
    log_tool_result("capture_stats", True, f"frames={stats.get('frames_received')}")
    return [types.TextContent(type="text", text=json.dumps(stats, indent=2))]


# ------------------------------------------------------------------
# Tool: get_signal_snapshot
# ------------------------------------------------------------------

@server.tool(
    name="get_signal_snapshot",
    description=(
        "Get the most recent decoded value of every signal from every module on the bus. "
        "This is the core multi-module view: a single call returns the current state "
        "of all signals simultaneously -- ECM, HVAC, BCM, TCM, and any other module "
        "broadcasting on the bus. Each entry includes signal name, parent message "
        "(module), current value, unit, and age in milliseconds."
    ),
)
async def get_signal_snapshot() -> list[types.TextContent]:
    log_tool_call("get_signal_snapshot", {})
    sniffer = _require_sniffer()
    import json
    snapshot = sniffer.signal_snapshot()
    log_tool_result("get_signal_snapshot", True, f"signals={len(snapshot)}")
    return [types.TextContent(
        type="text",
        text=json.dumps({"signal_count": len(snapshot), "signals": snapshot}, indent=2),
    )]


# ------------------------------------------------------------------
# Tool: get_signal_history
# ------------------------------------------------------------------

@server.tool(
    name="get_signal_history",
    description=(
        "Get the time-series history for one or more named signals. "
        "Returns timestamped samples captured since the sniffer started (or within "
        "last_n_seconds if specified). Use this to see how a signal changed over time."
    ),
)
async def get_signal_history(
    signal_names: list[str],
    last_n_seconds: float | None = None,
    max_samples: int = 200,
) -> list[types.TextContent]:
    """
    Args:
        signal_names: List of signal names exactly as they appear in the DBC.
        last_n_seconds: Only return samples from this window. None = all history.
        max_samples: Max samples per signal (decimated evenly if exceeded).
    """
    log_tool_call("get_signal_history",
                  {"signal_names": signal_names, "last_n_seconds": last_n_seconds,
                   "max_samples": max_samples})
    sniffer = _require_sniffer()
    import json
    history = sniffer.signal_history(signal_names, last_n_seconds, max_samples)
    log_tool_result("get_signal_history", True,
                    f"signals={list(history.keys())} "
                    f"samples={[len(v) for v in history.values()]}")
    return [types.TextContent(type="text", text=json.dumps(history, indent=2))]


# ------------------------------------------------------------------
# Tool: correlate_signals
# ------------------------------------------------------------------

@server.tool(
    name="correlate_signals",
    description=(
        "Align multiple signals from different modules onto a common time grid "
        "for cross-module correlation analysis. "
        "Use this to answer questions like: did the HVAC A/C Permission signal "
        "drop before or after the ECM A/C Pressure reading changed? "
        "Returns a time axis, aligned signal values, and detected value-change "
        "events sorted chronologically across all modules."
    ),
)
async def correlate_signals(
    signal_names: list[str],
    last_n_seconds: float = 30.0,
    resolution_ms: int = 100,
) -> list[types.TextContent]:
    """
    Args:
        signal_names: Signals to correlate -- can span multiple modules/messages.
        last_n_seconds: Time window to analyse (default 30s).
        resolution_ms: Time grid resolution in milliseconds (default 100ms).
    """
    log_tool_call("correlate_signals",
                  {"signal_names": signal_names, "last_n_seconds": last_n_seconds,
                   "resolution_ms": resolution_ms})
    sniffer = _require_sniffer()
    import json
    result = sniffer.correlate(signal_names, last_n_seconds, resolution_ms)
    log_tool_result("correlate_signals", True,
                    f"time_points={len(result.get('time_axis', []))} "
                    f"events={len(result.get('events', []))}")
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    acquire_instance_lock()
    set_source("MCP ")
    log_system_info()
    install_crash_handler()
    logging.basicConfig(level=getattr(logging, settings.mcp_log_level, logging.INFO))
    logger.info(
        "Starting %s (transport=%s)",
        settings.mcp_server_name,
        settings.transport.value,
    )
    log_event("SESSION_START", server=settings.mcp_server_name,
              transport=settings.transport.value)

    async def _run() -> None:
        async with lifespan(server):
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )

    try:
        asyncio.run(_run())
    finally:
        log_event("SESSION_END")


if __name__ == "__main__":
    main()

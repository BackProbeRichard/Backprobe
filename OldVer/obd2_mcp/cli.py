"""CLI entry point for quick testing and device management.

Usage:
    obd2-cli devices          # list available devices
    obd2-cli connect          # test connection
    obd2-cli dtcs             # read and print DTCs
    obd2-cli live             # print live data snapshot
    obd2-cli vin              # read VIN
"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.console import Console
from rich.table import Table

from obd2_mcp.config import settings

app = typer.Typer(help="OBD2 AI Tool CLI")
console = Console()


def _get_transport():
    from obd2_mcp.config import TransportType

    match settings.transport:
        case TransportType.J2534:
            from obd2_mcp.transport.j2534 import J2534Transport
            return J2534Transport()
        case TransportType.ELM327:
            from obd2_mcp.transport.elm327 import ELM327Transport
            return ELM327Transport()
        case TransportType.SOCKETCAN:
            from obd2_mcp.transport.socketcan import SocketCANTransport
            return SocketCANTransport()
        case TransportType.VIRTUAL:
            from obd2_mcp.transport.virtual import VirtualTransport
            return VirtualTransport()


@app.command()
def devices() -> None:
    """List available diagnostic devices."""

    async def _run():
        transport = _get_transport()
        devs = await transport.list_devices()
        console.print_json(json.dumps(devs))

    asyncio.run(_run())


@app.command()
def connect() -> None:
    """Test connection to the vehicle."""

    async def _run():
        transport = _get_transport()
        await transport.connect()
        console.print("[green]Connected![/green]")
        console.print_json(json.dumps(transport.device_info))
        await transport.disconnect()

    asyncio.run(_run())


@app.command()
def dtcs() -> None:
    """Read and display stored DTCs."""
    from obd2_mcp.protocols.obd2 import OBD2Protocol

    async def _run():
        transport = _get_transport()
        await transport.connect()
        obd2 = OBD2Protocol(transport)
        codes = await obd2.read_dtcs()
        if not codes:
            console.print("[green]No DTCs found.[/green]")
        else:
            for code in codes:
                console.print(f"  [red]{code}[/red]")
        await transport.disconnect()

    asyncio.run(_run())


@app.command()
def live() -> None:
    """Print a live data snapshot."""
    from obd2_mcp.protocols.obd2 import OBD2Protocol

    async def _run():
        transport = _get_transport()
        await transport.connect()
        obd2 = OBD2Protocol(transport)
        data = await obd2.live_data_snapshot()

        table = Table(title="Live Data Snapshot")
        table.add_column("PID", style="cyan")
        table.add_column("Name")
        table.add_column("Value", style="green")

        for entry in data:
            table.add_row(
                entry.get("pid", ""),
                entry.get("name", ""),
                entry.get("value", entry.get("raw_hex", entry.get("error", ""))),
            )
        console.print(table)
        await transport.disconnect()

    asyncio.run(_run())


@app.command()
def vin() -> None:
    """Read Vehicle Identification Number via UDS."""
    from obd2_mcp.protocols.uds import UDSProtocol

    async def _run():
        transport = _get_transport()
        await transport.connect()
        uds = UDSProtocol(transport)
        result = await uds.read_data_by_id(0xF190)
        console.print_json(json.dumps(result))
        await transport.disconnect()

    asyncio.run(_run())


if __name__ == "__main__":
    app()

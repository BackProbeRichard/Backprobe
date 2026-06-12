"""Tests using the VirtualTransport — no hardware required."""

import pytest
from obd2_mcp.transport.virtual import VirtualTransport
from obd2_mcp.protocols.obd2 import OBD2Protocol
from obd2_mcp.protocols.uds import UDSProtocol


@pytest.fixture
async def connected_transport():
    t = VirtualTransport()
    await t.connect()
    yield t
    await t.disconnect()


async def test_virtual_connect(connected_transport):
    assert connected_transport.is_connected
    assert "vin" in connected_transport.device_info


async def test_read_dtcs(connected_transport):
    obd2 = OBD2Protocol(connected_transport)
    codes = await obd2.read_dtcs()
    assert isinstance(codes, list)
    assert "P0300" in codes


async def test_clear_dtcs(connected_transport):
    obd2 = OBD2Protocol(connected_transport)
    success = await obd2.clear_dtcs()
    assert success is True
    codes_after = await obd2.read_dtcs()
    assert codes_after == []


async def test_live_data_snapshot(connected_transport):
    obd2 = OBD2Protocol(connected_transport)
    data = await obd2.live_data_snapshot()
    assert isinstance(data, list)
    assert len(data) > 0


async def test_read_rpm(connected_transport):
    obd2 = OBD2Protocol(connected_transport)
    result = await obd2.query_pid(0x0C)
    assert "error" not in result or result.get("raw_hex") is not None


async def test_read_vin_uds(connected_transport):
    uds = UDSProtocol(connected_transport)
    result = await uds.read_data_by_id(0xF190)
    assert result["did"] == hex(0xF190)

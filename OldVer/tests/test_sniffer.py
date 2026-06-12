"""Tests for CanSnifferProtocol using VirtualTransport — no hardware required.

The VirtualTransport emits realistic CAN broadcast frames on a python-can
virtual bus. These tests verify DBC loading, frame capture, signal decoding,
snapshot, history, and cross-module correlation.
"""

import asyncio
import os
import pytest

from obd2_mcp.transport.virtual import VirtualTransport
from obd2_mcp.protocols.sniffer import CanSnifferProtocol

# Path to the test DBC fixture
DBC_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "virtual_vehicle.dbc")


@pytest.fixture
async def connected_transport():
    t = VirtualTransport(channel="test_sniffer_bus")
    await t.connect()
    yield t
    await t.disconnect()


@pytest.fixture
async def sniffer(connected_transport):
    s = CanSnifferProtocol(connected_transport)
    s.load_dbc(DBC_PATH)
    yield s
    if s.is_running:
        await s.stop()


# ------------------------------------------------------------------
# DBC loading
# ------------------------------------------------------------------

def test_load_dbc_summary(connected_transport):
    s = CanSnifferProtocol(connected_transport)
    summary = s.load_dbc(DBC_PATH)
    assert summary["message_count"] == 5
    assert summary["signal_count"] > 0
    # Spot check known messages
    names = {m["name"] for m in summary["messages"]}
    assert "ECM_Engine_Data" in names
    assert "HVAC_Status" in names
    assert "BCM_Body_Status" in names


def test_list_signals_all(sniffer):
    signals = sniffer.list_signals()
    names = {s["signal"] for s in signals}
    assert "ECM_RPM" in names
    assert "HVAC_AC_Permission" in names
    assert "ECM_AC_Pressure" in names
    assert "BCM_AC_Relay" in names


def test_find_signals_by_keyword(sniffer):
    ac_signals = sniffer.list_signals("ac")
    names = {s["signal"] for s in ac_signals}
    assert "HVAC_AC_Permission" in names
    assert "ECM_AC_Pressure" in names
    assert "ECM_AC_Request" in names


def test_find_signals_no_match(sniffer):
    results = sniffer.list_signals("XYZNOTEXIST")
    assert results == []


# ------------------------------------------------------------------
# Capture lifecycle
# ------------------------------------------------------------------

async def test_start_stop(sniffer):
    assert not sniffer.is_running
    await sniffer.start()
    assert sniffer.is_running
    await sniffer.stop()
    assert not sniffer.is_running


async def test_double_start_is_safe(sniffer):
    await sniffer.start()
    await sniffer.start()  # Should not raise or create duplicate tasks
    assert sniffer.is_running
    await sniffer.stop()


# ------------------------------------------------------------------
# Signal decoding
# ------------------------------------------------------------------

async def test_signals_decoded_after_capture(sniffer):
    """After a short capture period, decoded signals should appear in snapshot."""
    await sniffer.start()
    # Give the virtual transport time to emit several frames
    await asyncio.sleep(0.3)
    await sniffer.stop()

    snapshot = sniffer.signal_snapshot()
    assert len(snapshot) > 0

    signal_names = {s["signal"] for s in snapshot}
    # ECM, HVAC, BCM, and TCM should all have broadcast frames by now
    assert "ECM_RPM" in signal_names
    assert "HVAC_AC_Permission" in signal_names
    assert "BCM_AC_Relay" in signal_names


async def test_snapshot_has_expected_fields(sniffer):
    await sniffer.start()
    await asyncio.sleep(0.15)

    snapshot = sniffer.signal_snapshot()
    assert len(snapshot) > 0

    for entry in snapshot:
        assert "signal" in entry
        assert "message" in entry
        assert "value" in entry
        assert "timestamp" in entry
        assert "age_ms" in entry


async def test_ecm_rpm_in_expected_range(sniffer):
    """Virtual transport simulates RPM between 800–1000."""
    await sniffer.start()
    await asyncio.sleep(0.2)

    snapshot = sniffer.signal_snapshot()
    rpm_entry = next((s for s in snapshot if s["signal"] == "ECM_RPM"), None)
    assert rpm_entry is not None
    assert 700 <= rpm_entry["value"] <= 1100, f"RPM out of range: {rpm_entry['value']}"


# ------------------------------------------------------------------
# History
# ------------------------------------------------------------------

async def test_signal_history_populated(sniffer):
    await sniffer.start()
    await asyncio.sleep(0.3)
    await sniffer.stop()

    history = sniffer.signal_history(["ECM_RPM"])
    assert "ECM_RPM" in history
    assert len(history["ECM_RPM"]) > 0

    first = history["ECM_RPM"][0]
    assert "timestamp" in first
    assert "value" in first


async def test_signal_history_unknown_signal(sniffer):
    await sniffer.start()
    await asyncio.sleep(0.1)
    await sniffer.stop()

    history = sniffer.signal_history(["DOES_NOT_EXIST"])
    assert history["DOES_NOT_EXIST"] == []


async def test_signal_history_time_window(sniffer):
    await sniffer.start()
    await asyncio.sleep(0.3)

    # Requesting last 0.1s should return fewer samples than all history
    all_hist = sniffer.signal_history(["ECM_RPM"])
    recent_hist = sniffer.signal_history(["ECM_RPM"], last_n_seconds=0.05)

    all_count = len(all_hist["ECM_RPM"])
    recent_count = len(recent_hist["ECM_RPM"])
    assert recent_count <= all_count
    await sniffer.stop()


# ------------------------------------------------------------------
# Correlation
# ------------------------------------------------------------------

async def test_correlate_returns_structure(sniffer):
    await sniffer.start()
    await asyncio.sleep(0.25)

    result = sniffer.correlate(
        ["ECM_AC_Pressure", "HVAC_AC_Permission"],
        last_n_seconds=0.2,
        resolution_ms=50,
    )

    assert "time_axis" in result
    assert "signals" in result
    assert "events" in result
    assert "ECM_AC_Pressure" in result["signals"]
    assert "HVAC_AC_Permission" in result["signals"]
    assert isinstance(result["time_axis"], list)
    await sniffer.stop()


async def test_correlate_time_axis_length_matches_signals(sniffer):
    await sniffer.start()
    await asyncio.sleep(0.2)

    result = sniffer.correlate(
        ["ECM_RPM", "HVAC_AC_Permission"],
        last_n_seconds=0.1,
        resolution_ms=10,
    )

    n = len(result["time_axis"])
    for sig_name, values in result["signals"].items():
        assert len(values) == n, f"{sig_name} length mismatch"

    await sniffer.stop()


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------

async def test_stats_after_capture(sniffer):
    await sniffer.start()
    await asyncio.sleep(0.2)
    await sniffer.stop()

    stats = sniffer.stats()
    assert stats["running"] is False
    assert stats["frames_received"] > 0
    assert stats["frames_decoded"] > 0
    assert stats["signals_tracked"] > 0
    assert stats["dbc_loaded"] == DBC_PATH


async def test_stats_unknown_ids_empty_with_full_dbc(sniffer):
    """All virtual transport IDs should be in the DBC — no unknowns."""
    await sniffer.start()
    await asyncio.sleep(0.2)
    await sniffer.stop()

    stats = sniffer.stats()
    assert stats["frames_unknown"] == 0, (
        f"Unexpected unknown IDs: {stats['unknown_ids']}"
    )

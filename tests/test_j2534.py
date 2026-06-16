"""J2534 backend tests. No hardware — FakeJ2534Lib simulates vehicle responses.

Proves the same logical paths as test_virtual.py but now exercised through
the full J2534 abstraction: filter install, LOOPBACK config, the read loop
(echo skip, first-frame skip), SID/PID backstop, teardown order, and error
mapping. Hardware-only verification is in PHASE_1_J2534_DESIGN.md §Bench.
"""

import pytest

from backprobe.obd2 import decode
from backprobe.transport import j2534 as j
from backprobe.transport import virtual as v
from backprobe.transport.base import (
    ConnectError,
    ConnectProfile,
    DeviceLost,
    Timeout,
)

P11_500 = ConnectProfile("ISO15765", 500_000, "11-bit")
P29_500 = ConnectProfile("ISO15765", 500_000, "29-bit")
P11_250 = ConnectProfile("ISO15765", 250_000, "11-bit")


def _backend(preset_name: str, **fake_kwargs) -> tuple[j.J2534Backend, j.FakeJ2534Lib]:
    lib = j.FakeJ2534Lib(v.get_preset(preset_name), _noop_delay_ms=0, **fake_kwargs)
    return j.J2534Backend(lib=lib), lib


def _open(preset_name: str, **fake_kwargs):
    backend, lib = _backend(preset_name, **fake_kwargs)
    session = backend.open(backend.enumerate()[0])
    return session, lib


# ─── enumerate / open / info / voltage ────────────────────────────────────────


def test_enumerate_returns_one_fake_device():
    backend, _ = _backend("sedan_mil_on")
    assert len(backend.enumerate()) == 1


def test_open_returns_session_with_fake_version():
    session, _ = _open("sedan_mil_on")
    info = session.info()
    assert info.api_version == "04.04"
    assert info.firmware == "fake-1.0"


def test_read_voltage_returns_fake_vbatt():
    session, _ = _open("sedan_mil_on", vbatt_mv=12_847)
    assert session.read_voltage() == 12_847


def test_close_is_idempotent():
    session, _ = _open("sedan_mil_on")
    session.close()
    session.close()


def test_read_voltage_after_close_raises():
    session, _ = _open("sedan_mil_on")
    session.close()
    with pytest.raises(DeviceLost):
        session.read_voltage()


# ─── connect / profile mismatch ───────────────────────────────────────────────


def test_connect_matching_profile_succeeds():
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    assert ch is not None


def test_connect_wrong_bitrate_raises():
    session, _ = _open("sedan_mil_on")
    with pytest.raises(ConnectError):
        session.connect(P11_250)  # sedan wants 500k


def test_connect_wrong_addressing_raises():
    session, _ = _open("sedan_mil_on")
    with pytest.raises(ConnectError):
        session.connect(P29_500)  # sedan is 11-bit


def test_29bit_vehicle_rejects_11bit_profiles():
    session, _ = _open("diesel_29bit")
    for wrong in (P11_500, P11_250):
        with pytest.raises(ConnectError):
            session.connect(wrong)
    ch = session.connect(P29_500)
    assert ch is not None


def test_connect_after_session_close_raises():
    session, _ = _open("sedan_mil_on")
    session.close()
    with pytest.raises(DeviceLost):
        session.connect(P11_500)


# ─── ask — the primary single-reply path ──────────────────────────────────────


def test_ask_vin_returns_correct_vin():
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    reply = ch.ask(b"\x09\x02", 0.1)
    assert decode.decode_vin(reply.payload) == "1HGCM82633A004321"
    assert reply.ecu == 0x7E8


def test_ask_mil_status_returns_mil_on():
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    reply = ch.ask(b"\x01\x01", 0.1)
    mil, count = decode.decode_mil_status(reply.payload)
    assert mil is True
    assert count == 2


def test_ask_confirm_0100_succeeds():
    """The probe confirming query: should get a support-mask reply."""
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    reply = ch.ask(b"\x01\x00", 0.1)
    _, sup, _ = decode.decode_supported_pids(reply.payload)
    assert len(sup) > 0


def test_ask_unsupported_pid_times_out():
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    with pytest.raises(Timeout):
        ch.ask(b"\x01\xfe", 0.02)  # PID 0xFE — nobody answers


def test_ask_29bit_ecu_has_correct_address():
    session, _ = _open("diesel_29bit")
    ch = session.connect(P29_500)
    reply = ch.ask(b"\x01\x00", 0.1)
    assert reply.ecu == 0x18DAF110


# ─── ask_all — the census / drain-window path ─────────────────────────────────


def test_ask_all_single_ecu():
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    replies = ch.ask_all(b"\x01\x00", 0.02)
    assert [r.ecu for r in replies] == [0x7E8]


def test_ask_all_multi_ecu_census():
    session, _ = _open("truck_healthy")
    ch = session.connect(P11_500)
    replies = ch.ask_all(b"\x01\x00", 0.02)
    assert sorted(r.ecu for r in replies) == [0x7E8, 0x7E9]


def test_ask_all_unsupported_returns_empty():
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    result = ch.ask_all(b"\x01\xfe", 0.02)
    assert result == []


def test_ask_all_does_not_duplicate_ecus():
    """ask_all must collect one reply per ECU even if the ECU sends multiple."""
    session, _ = _open("truck_healthy")
    ch = session.connect(P11_500)
    replies = ch.ask_all(b"\x01\x00", 0.02)
    ecus = [r.ecu for r in replies]
    assert len(ecus) == len(set(ecus))


# ─── disconnect ───────────────────────────────────────────────────────────────


def test_disconnect_is_idempotent():
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    ch.disconnect()
    ch.disconnect()


def test_ask_after_disconnect_raises():
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    ch.disconnect()
    with pytest.raises(DeviceLost):
        ch.ask(b"\x09\x02", 0.1)


def test_ask_all_after_disconnect_raises():
    session, _ = _open("sedan_mil_on")
    ch = session.connect(P11_500)
    ch.disconnect()
    with pytest.raises(DeviceLost):
        ch.ask_all(b"\x01\x00", 0.02)


# ─── RX clear / SID‑PID backstop (Decision 3) ────────────────────────────────


class _NoRxClear(j.FakeJ2534Lib):
    """Simulates a device that does not support CLEAR_RX_BUFFER."""

    def ioctl_clear_rx(self, channel_id: int) -> str | None:
        return "ERR_NOT_SUPPORTED"


def test_rx_clear_unsupported_flag_set():
    lib = _NoRxClear(v.get_preset("sedan_mil_on"), _noop_delay_ms=0)
    session = j.J2534Backend(lib=lib).open(
        j.J2534Backend(lib=lib).enumerate()[0]
    )
    ch = session.connect(P11_500)
    ch.ask(b"\x01\x00", 0.1)  # triggers the first clear attempt
    assert ch.rx_clear_unsupported is True
    assert ch._rx_clear_code == "ERR_NOT_SUPPORTED"


def test_rx_clear_unsupported_ask_still_works():
    """SID/PID matching compensates when clear is unavailable."""
    lib = _NoRxClear(v.get_preset("sedan_mil_on"), _noop_delay_ms=0)
    backend = j.J2534Backend(lib=lib)
    session = backend.open(backend.enumerate()[0])
    ch = session.connect(P11_500)
    reply = ch.ask(b"\x09\x02", 0.1)  # VIN request
    assert decode.decode_vin(reply.payload) == "1HGCM82633A004321"


class _StaleFrameLib(j.FakeJ2534Lib):
    """Injects a stale MIL response into the queue before the real VIN response."""

    def __init__(self, vehicle, stale: bytes) -> None:
        super().__init__(vehicle, _noop_delay_ms=0)
        self._stale = stale
        self._injected = False

    def ioctl_clear_rx(self, channel_id: int) -> str | None:
        return "ERR_NOT_SUPPORTED"  # force SID/PID backstop to be the only guard

    def write_msg(self, channel_id, data, tx_flags, timeout_ms):
        if not self._injected:
            self._injected = True
            self._rx_queue.append((0, (0x7E8).to_bytes(4, "big") + self._stale))
        super().write_msg(channel_id, data, tx_flags, timeout_ms)


def test_stale_frame_rejected_by_sid_pid_backstop():
    """A leftover MIL reply (0x41 0x01 ...) is rejected when we ask for VIN (→ 0x49 0x02)."""
    stale = bytes.fromhex("41018207e100")  # real MIL frame
    lib   = _StaleFrameLib(v.get_preset("sedan_mil_on"), stale)
    backend = j.J2534Backend(lib=lib)
    session = backend.open(backend.enumerate()[0])
    ch = session.connect(P11_500)
    reply = ch.ask(b"\x09\x02", 0.5)
    assert decode.decode_vin(reply.payload) == "1HGCM82633A004321"


# ─── probe integration (Decision 4 — full probe list via interrogate) ─────────


def test_probe_succeeds_with_fake_lib(tmp_path):
    from backprobe.daemon import events as ev
    from backprobe.daemon import interrogate as intr

    lib = j.FakeJ2534Lib(v.get_preset("sedan_mil_on"), _noop_delay_ms=0)
    backend = j.J2534Backend(lib=lib)
    session = backend.open(backend.enumerate()[0])
    event_log = ev.EventLog(tmp_path / "logs", console=False)

    result = intr.probe(session, event_log)
    assert result is not None
    assert result.profile == P11_500
    assert result.step == 1  # wins on the first profile


def test_probe_29bit_skips_11bit_profiles(tmp_path):
    from backprobe.daemon import events as ev
    from backprobe.daemon import interrogate as intr

    lib = j.FakeJ2534Lib(v.get_preset("diesel_29bit"), _noop_delay_ms=0)
    backend = j.J2534Backend(lib=lib)
    session = backend.open(backend.enumerate()[0])
    event_log = ev.EventLog(tmp_path / "logs", console=False)

    result = intr.probe(session, event_log)
    assert result is not None
    assert result.profile.addressing == "29-bit"
    assert result.step == 2  # step 2 is 29-bit/500k in PROBE_PROFILES


def test_rx_clear_unsupported_emitted_to_event_log(tmp_path):
    """When the winning channel has rx_clear_unsupported, interrogate.probe()
    emits rx_clear_unsupported to the event log (Decision 3 — both logs)."""
    import json
    from backprobe.daemon import events as ev
    from backprobe.daemon import interrogate as intr

    lib = _NoRxClear(v.get_preset("sedan_mil_on"), _noop_delay_ms=0)
    backend = j.J2534Backend(lib=lib)
    session = backend.open(backend.enumerate()[0])
    event_log = ev.EventLog(tmp_path / "logs", console=False)

    result = intr.probe(session, event_log)
    assert result is not None

    events = [json.loads(ln) for ln in event_log.path.read_text().splitlines()]
    event_names = [e["event"] for e in events]
    assert "rx_clear_unsupported" in event_names
    rx_ev = next(e for e in events if e["event"] == "rx_clear_unsupported")
    assert rx_ev["j2534_code"] == "ERR_NOT_SUPPORTED"

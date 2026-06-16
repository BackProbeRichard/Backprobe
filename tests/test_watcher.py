"""Watcher state-machine tests, against the virtual backend. No hardware.

Covers the full SCANNING→HOLDING→INTERROGATING→ATTACHED→(drop)→HOLDING cycle,
device-loss recovery to SCANNING (Decision 6), clean release, and the run()
loop + stop. Handlers are single-step, so most tests need no threads.
"""

import json
import threading
import time

import pytest

from backprobe import session_log
from backprobe.daemon.watcher import State, Watcher
from backprobe.transport import virtual as v
from backprobe.transport.base import Device, DeviceLost, TransportBackend


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path):
    session_log._log_file = None
    session_log.init(tmp_path / "diag")


def _watcher(tmp_path, *, attached=None, debounce=2, **kw):
    backend = v.VirtualBackend(attached=v.get_preset(attached) if attached else None)
    return Watcher(backend, events_dir=str(tmp_path / "events"), console=False,
                   install_stop_triggers=False, debounce=debounce, **kw)


def _events(w) -> list[dict]:
    return [json.loads(ln) for ln in w._events.path.read_text().splitlines()]


def _kinds(w) -> list[str]:
    return [e["event"] for e in _events(w)]


def _vsession(w: Watcher) -> v.VirtualSession:
    assert isinstance(w._session, v.VirtualSession)
    return w._session


# ─── full lifecycle via step() ──────────────────────────────────────────────


def test_scanning_opens_device_and_holds(tmp_path):
    w = _watcher(tmp_path)
    assert w.step() is State.HOLDING
    assert "device_opened" in _kinds(w)


def test_holding_debounces_before_interrogating(tmp_path):
    w = _watcher(tmp_path, debounce=2)
    w.step()                                            # -> HOLDING
    _vsession(w).plug_in(v.get_preset("sedan_mil_on"))
    assert w.step() is State.HOLDING                    # streak 1, not yet
    assert w.step() is State.INTERROGATING              # streak 2 -> go
    assert "voltage_detected" in _kinds(w)


def test_full_cycle_to_attached_and_back(tmp_path):
    w = _watcher(tmp_path, debounce=1)
    w.step()                                            # HOLDING
    _vsession(w).plug_in(v.get_preset("sedan_mil_on"))
    assert w.step() is State.INTERROGATING
    assert w.step() is State.ATTACHED
    _vsession(w).unplug()
    assert w.step() is State.HOLDING                    # drop (debounce 1)
    kinds = _kinds(w)
    for e in ("connection_complete", "voltage_dropped", "channel_closed"):
        assert e in kinds
    cc = [e for e in _events(w) if e["event"] == "connection_complete"][0]
    assert cc["mil_on"] is True and cc["ecu_count"] == 1


def test_interrogate_with_no_winning_probe_returns_to_holding(tmp_path, monkeypatch):
    from backprobe.daemon import interrogate
    w = _watcher(tmp_path, debounce=1)
    w.step()
    _vsession(w).plug_in(v.get_preset("sedan_mil_on"))
    w.step()                                            # -> INTERROGATING
    monkeypatch.setattr(interrogate, "probe", lambda *a, **k: None)
    assert w.step() is State.HOLDING


# ─── device-loss recovery (Decision 6) ──────────────────────────────────────


def test_device_lost_in_holding_recovers_to_scanning(tmp_path):
    w = _watcher(tmp_path)
    w.step()                                            # -> HOLDING, session open
    def boom():
        raise DeviceLost("yanked")
    _vsession(w).read_voltage = boom                    # device vanishes
    assert w.step() is State.SCANNING
    kinds = _kinds(w)
    assert "device_lost" in kinds and "device_released" in kinds
    assert w._session is None                            # handle dropped


def test_device_lost_while_attached_recovers(tmp_path):
    w = _watcher(tmp_path, debounce=1)
    w.step()
    _vsession(w).plug_in(v.get_preset("truck_healthy"))
    w.step()  # -> INTERROGATING
    w.step()  # -> ATTACHED
    assert w._state is State.ATTACHED
    _vsession(w).read_voltage = lambda: (_ for _ in ()).throw(DeviceLost("usb pulled"))
    assert w.step() is State.SCANNING
    assert w._channel is None and w._session is None


# ─── release ────────────────────────────────────────────────────────────────


def test_release_is_idempotent_and_emits_device_released(tmp_path):
    w = _watcher(tmp_path, debounce=1)
    w.step()
    _vsession(w).plug_in(v.get_preset("sedan_mil_on"))
    w.step()  # -> INTERROGATING
    w.step()  # -> ATTACHED
    w._release()
    w._release()                                        # idempotent
    assert w._session is None and w._channel is None
    assert _kinds(w).count("device_released") == 1


# ─── run() loop + stop ──────────────────────────────────────────────────────


def test_run_drives_to_attached_then_stops_clean(tmp_path):
    w = _watcher(tmp_path, attached="sedan_mil_on", debounce=1, poll_s=0.01)
    t = threading.Thread(target=w.run)
    t.start()
    deadline = time.time() + 3.0
    while w._state is not State.ATTACHED and time.time() < deadline:
        time.sleep(0.01)
    assert w._state is State.ATTACHED
    w.request_stop()
    t.join(timeout=3.0)
    assert not t.is_alive()
    kinds = _kinds(w)
    assert kinds[0] == "daemon_start" and kinds[-1] == "daemon_stop"
    assert "device_released" in kinds
    assert _events(w)[-1]["vehicles_logged"] == 1


# ─── multi-device scanning ───────────────────────────────────────────────────


def test_scanning_skips_unreachable_devices_and_opens_last(tmp_path):
    """_scanning() must iterate all devices and skip ones that raise DeviceLost,
    opening the first reachable device. Regression for the bench bug where
    12 DLLs were registered and devices[0] was unreachable."""
    real_backend = v.VirtualBackend()
    real_device  = real_backend.enumerate()[0]
    dead_a = Device(vendor="Autel", name="MaxiFlash Bluetooth", address="fake://0")
    dead_b = Device(vendor="Autel", name="MaxiFlash VCMI",      address="fake://1")

    class _MultiBackend(TransportBackend):
        def enumerate(self):
            return [dead_a, dead_b, real_device]

        def open(self, device):
            if device.address.startswith("fake://"):
                raise DeviceLost(f"not connected: {device.name}")
            return real_backend.open(device)

    w = Watcher(
        _MultiBackend(),
        events_dir=str(tmp_path / "events"),
        console=False,
        install_stop_triggers=False,
    )
    assert w.step() is State.HOLDING
    kinds = _kinds(w)
    assert "device_opened" in kinds
    opened = next(e for e in _events(w) if e["event"] == "device_opened")
    assert "Virtual" in opened["device"]

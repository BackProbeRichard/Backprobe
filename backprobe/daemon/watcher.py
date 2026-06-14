"""The Watcher — the daemon state machine (PHASE_1_DAEMON_DESIGN).

SCANNING → HOLDING → INTERROGATING → ATTACHED, looping forever, releasing the
device on every exit. Decisions in force:

  1. Explicit State enum + handler dispatch; each handler returns the next state.
  2. The transport backend is injected (__main__ picks J2534 or virtual).
  3. Voltage presence uses hysteresis + a debounce streak (no flapping).
  6. Many stop triggers → one request_stop() → one idempotent release(); Ctrl-C
     is neutralized; a device that vanishes recovers to SCANNING, never exits.

Handlers are single-step (one read / one unit of work) so the loop is testable
without threads or sleeps — run() adds the poll cadence. DeviceLost from any
handler is caught centrally: release, then SCANNING.
"""

from __future__ import annotations

import enum
import os
import platform as _platform
import sys
import time

from backprobe import session_log
from backprobe.daemon import interrogate
from backprobe.daemon.events import EventLog
from backprobe.transport.base import (
    Device,
    DeviceBusy,
    DeviceLost,
    InternalError,
    TransportBackend,
)


class State(enum.Enum):
    SCANNING = "SCANNING"           # no device held
    HOLDING = "HOLDING"             # device held, waiting for a vehicle
    INTERROGATING = "INTERROGATING"  # vehicle present, probe + harvest
    ATTACHED = "ATTACHED"          # connected, watching for unplug


_IDLE_STATES = {State.SCANNING, State.HOLDING, State.ATTACHED}  # loop polls these at cadence


class Watcher:
    """Owns the device while running; drives the interrogation; writes events."""

    def __init__(self, backend: TransportBackend, *, events_dir: str = "logs",
                 poll_s: float = 1.0, present_mv: int = 9000, absent_mv: int = 7000,
                 debounce: int = 2, console: bool = True,
                 install_stop_triggers: bool = True) -> None:
        self._backend = backend
        self._events = EventLog(events_dir, console=console)
        self._poll_s = poll_s
        self._present_mv = present_mv
        self._absent_mv = absent_mv
        self._debounce = debounce
        self._console = console
        self._install_triggers = install_stop_triggers
        self._log = session_log.get_logger("WATCHER")

        self._state = State.SCANNING
        self._session = None
        self._channel = None
        self._device_id = 1
        self._device_name = "device"
        self._present_streak = 0
        self._absent_streak = 0
        self._conn_start: float | None = None
        self._vehicles = 0
        self._stop = False

    # ─── public lifecycle ───────────────────────────────────────────────────

    def run(self) -> int:
        """The daemon loop. Returns an exit code. Releases the device no matter
        how it ends (Decision 6)."""
        started = time.time()
        self._emit_daemon_start()
        if self._install_triggers:
            self._install_stop_triggers()
        try:
            while not self._stop:
                self.step()
                if self._quit_key_pressed():
                    self.request_stop()
                if not self._stop and self._state in _IDLE_STATES:
                    time.sleep(self._poll_s)
        finally:
            self._release()
            self._events.daemon_stop(uptime_seconds=time.time() - started,
                                     vehicles_logged=self._vehicles, exit_code=0)
            self._events.close()
        return 0

    def step(self) -> State:
        """Run the current state's handler once. DeviceLost from any state →
        release → SCANNING (Decision 6 recovery)."""
        prev = self._state
        try:
            nxt = _HANDLERS[self._state](self)
        except DeviceLost:
            self._on_device_lost()
            nxt = State.SCANNING
        if nxt is not prev:
            self._log.event("STATE", **{"from": prev.value, "to": nxt.value})
            if self._console:
                print(f"[state] {prev.value} → {nxt.value}")
        self._state = nxt
        return nxt

    def request_stop(self) -> None:
        """Trip a clean shutdown. Idempotent. Called by every stop trigger."""
        if not self._stop:
            self._stop = True
            self._log.event("STOP_REQUESTED")

    # ─── state handlers (each returns the next state) ───────────────────────

    def _scanning(self) -> State:
        devices = self._backend.enumerate()
        if not devices:
            return State.SCANNING                          # wait; loop sleeps
        try:
            self._session = self._backend.open(devices[0])
        except DeviceBusy:
            self._log.event("DEVICE_BUSY", device=devices[0].name)
            return State.SCANNING                           # wait politely, retry
        except (DeviceLost, InternalError) as exc:
            self._log.error("OPEN_FAILED", exc, device=devices[0].name)
            return State.SCANNING
        report = self._session.info()
        self._device_name = report.name
        self._events.device_opened(device=report.name, device_id=self._device_id,
                                   firmware=report.firmware,
                                   dll_version=report.dll_version)
        self._present_streak = 0
        return State.HOLDING

    def _holding(self) -> State:
        mv = self._session.read_voltage()                  # may raise DeviceLost
        self._present_streak = self._present_streak + 1 if mv >= self._present_mv else 0
        if self._present_streak >= self._debounce:
            self._events.voltage_detected(voltage_mv=mv)
            self._conn_start = time.perf_counter()
            return State.INTERROGATING
        return State.HOLDING

    def _interrogating(self) -> State:
        result = interrogate.probe(self._session, self._events)
        if result is None:
            return State.HOLDING                            # no vehicle won the probe
        self._channel = result.channel                      # track for teardown ASAP
        summary = interrogate.harvest(self._session, result.channel, self._events)
        if summary is None:                                 # census found no ECU
            self._disconnect_channel()
            return State.HOLDING
        duration = (time.perf_counter() - self._conn_start) * 1000 if self._conn_start else 0.0
        self._events.connection_complete(
            vin=summary.vin, protocol=interrogate.protocol_label(result.profile),
            ecu_count=summary.ecu_count, mil_on=bool(summary.mil_on),
            stored_dtc_count=summary.dtc_count or 0,
            mode_06_supported=summary.mode_06_supported, connected_duration_ms=duration)
        self._vehicles += 1
        self._absent_streak = 0
        return State.ATTACHED

    def _attached(self) -> State:
        mv = self._session.read_voltage()                  # may raise DeviceLost
        self._absent_streak = self._absent_streak + 1 if mv < self._absent_mv else 0
        if self._absent_streak >= self._debounce:
            self._events.voltage_dropped(voltage_mv=mv)
            self._disconnect_channel()
            self._present_streak = 0
            return State.HOLDING
        return State.ATTACHED

    # ─── teardown (Decision 6) ──────────────────────────────────────────────

    def _disconnect_channel(self) -> None:
        """Drop the live connection, keep the device held. Idempotent."""
        if self._channel is not None:
            try:
                self._channel.disconnect()
            except Exception:                               # never let teardown raise
                pass
            self._events.channel_closed(device_id=self._device_id)
            self._channel = None

    def _release(self) -> None:
        """The one idempotent, dead-handle-tolerant release: disconnect → close
        (sacred order). Used by both clean exit and device-loss recovery. Never
        blocks, never raises."""
        if self._channel is not None:
            try:
                self._channel.disconnect()
            except Exception:
                pass
            self._channel = None
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._events.device_released(device=self._device_name)
            self._session = None

    def _on_device_lost(self) -> None:
        """The device vanished under us — recover, don't exit (Decision 6)."""
        self._log.event("DEVICE_LOST", device=self._device_name)
        self._events.device_lost(device=self._device_name)
        self._release()
        self._present_streak = self._absent_streak = 0

    # ─── stop triggers (Decision 6) ─────────────────────────────────────────

    def _install_stop_triggers(self) -> None:
        """Wire every stop affordance to request_stop(); neutralize Ctrl-C."""
        import signal
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)   # Ctrl-C does nothing
        except Exception:
            pass
        for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):     # OS stop / window close (Unix)
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: self.request_stop())
                except Exception:
                    pass
        self._install_console_ctrl_handler()               # window close (Windows)
        if self._console:
            print("Watcher running — press 'q' or close the window to stop "
                  "(Ctrl-C is disabled).")

    def _install_console_ctrl_handler(self) -> None:
        """Windows: catch the console-close event so a window close releases the
        device. No-op elsewhere; best-effort (never fatal)."""
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
            def _handler(ctrl_type):                       # CTRL_CLOSE_EVENT etc.
                self.request_stop()
                return True

            self._console_ctrl_ref = _handler              # keep a ref alive
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True)
        except Exception:
            pass

    def _quit_key_pressed(self) -> bool:
        """Non-blocking check for a 'q' keypress. Tolerant of non-tty stdin."""
        try:
            if os.name == "nt":
                import msvcrt
                if msvcrt.kbhit():
                    return msvcrt.getch() in (b"q", b"Q")
                return False
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                return sys.stdin.readline().strip().lower() == "q"
            return False
        except Exception:
            return False

    # ─── events ─────────────────────────────────────────────────────────────

    def _emit_daemon_start(self) -> None:
        devices = self._backend.enumerate()
        self._events.daemon_start(
            python=_platform.python_version(), platform=_platform.platform(),
            devices=[{"vendor": d.vendor, "name": d.name, "address": d.address}
                     for d in devices])


_HANDLERS = {
    State.SCANNING: Watcher._scanning,
    State.HOLDING: Watcher._holding,
    State.INTERROGATING: Watcher._interrogating,
    State.ATTACHED: Watcher._attached,
}

"""Passive CAN bus sniffer with DBC-based signal decoding.

Uses python-can's async Notifier + cantools to capture and decode every frame
from every module on the bus simultaneously — no polling, no sequential querying.

This is the capability that standard scan tools lack: synchronized, timestamped
data from all modules at once. Feed it a DBC file and Claude can see, for example,
that the HVAC module's A/C Permission signal dropped 2.3 seconds *after* the ECM's
A/C pressure sensor reading changed — pointing at a calibration mismatch rather than
a refrigerant issue.

Architecture:
  CanSnifferProtocol
    ├── load_dbc()          — parse a DBC/KCD/SYM file with cantools
    ├── start()             — attach can.Notifier to the transport's raw bus
    ├── stop()              — detach Notifier, cancel background task
    ├── signal_snapshot()   — latest decoded value of every known signal
    ├── signal_history()    — time-series for one or more named signals
    └── correlate()         — aligned multi-signal view for cross-module analysis
"""

from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass, field
from typing import Any

import can
import cantools
import cantools.database

from obd2_mcp.transport.base import BaseTransport


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SignalSample:
    timestamp: float       # monotonic seconds since epoch (time.monotonic())
    message_name: str
    signal_name: str
    raw_value: Any         # numeric or string (for named values in DBC)
    unit: str | None


@dataclass
class SnifferStats:
    frames_received: int = 0
    frames_decoded: int = 0
    frames_unknown: int = 0
    unknown_ids: set[int] = field(default_factory=set)
    start_time: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.start_time


# Maximum samples kept per signal in the history ring buffer
_HISTORY_DEPTH = 2000


class CanSnifferProtocol:
    """Passive CAN bus monitor with DBC signal decoding.

    Usage::

        sniffer = CanSnifferProtocol(transport)
        sniffer.load_dbc("/path/to/vehicle.dbc")
        await sniffer.start()
        await asyncio.sleep(10)
        snapshot = sniffer.signal_snapshot()
        correlated = sniffer.correlate(["ECM_AC_Pressure", "HVAC_AC_Permission"])
        await sniffer.stop()
    """

    def __init__(self, transport: BaseTransport) -> None:
        self._transport = transport
        self._db: cantools.database.Database | None = None
        self._db_path: str | None = None

        # signal_name → deque of SignalSample (ring buffer)
        self._history: dict[str, collections.deque[SignalSample]] = {}
        # signal_name → most recent SignalSample
        self._latest: dict[str, SignalSample] = {}

        self._stats = SnifferStats()
        self._running = False
        self._notifier: can.Notifier | None = None
        self._reader: can.AsyncBufferedReader | None = None
        self._capture_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # DBC management
    # ------------------------------------------------------------------

    def load_dbc(self, path: str) -> dict[str, Any]:
        """Load a DBC (or KCD/SYM) file.

        Returns a summary dict with message and signal counts.
        Raises FileNotFoundError or cantools.database.UnsupportedDatabaseFormatError
        on bad input.
        """
        db = cantools.database.load_file(path)
        self._db = db
        self._db_path = path

        messages = []
        for msg in db.messages:
            messages.append({
                "id": hex(msg.frame_id),
                "name": msg.name,
                "length": msg.length,
                "signals": [s.name for s in msg.signals],
            })

        return {
            "path": path,
            "message_count": len(db.messages),
            "signal_count": sum(len(m.signals) for m in db.messages),
            "messages": messages,
        }

    def list_signals(self, keyword: str | None = None) -> list[dict[str, Any]]:
        """List all signals in the loaded DBC, optionally filtered by keyword.

        Returns signal metadata: name, message, unit, min, max, choices.
        """
        if self._db is None:
            return []

        results = []
        kw = keyword.lower() if keyword else None

        for msg in self._db.messages:
            for sig in msg.signals:
                name_match = kw is None or kw in sig.name.lower()
                msg_match = kw is None or kw in msg.name.lower()
                if name_match or msg_match:
                    results.append({
                        "signal": sig.name,
                        "message": msg.name,
                        "message_id": hex(msg.frame_id),
                        "unit": sig.unit or None,
                        "min": sig.minimum,
                        "max": sig.maximum,
                        "choices": sig.choices or None,
                    })
        return results

    # ------------------------------------------------------------------
    # Capture lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start passive capture on the transport's raw CAN bus."""
        if self._running:
            return

        bus = self._transport.get_raw_bus()
        if bus is None:
            raise RuntimeError(
                "This transport does not expose a raw CAN bus. "
                "Use J2534Transport, SocketCANTransport, or VirtualTransport."
            )

        self._stats = SnifferStats()
        self._reader = can.AsyncBufferedReader()
        loop = asyncio.get_event_loop()

        # Notifier pushes received frames into the AsyncBufferedReader
        self._notifier = can.Notifier(bus, [self._reader], loop=loop)
        self._running = True
        self._capture_task = asyncio.create_task(
            self._decode_loop(), name="can_sniffer"
        )

    async def stop(self) -> None:
        """Stop passive capture and detach from the bus."""
        self._running = False

        if self._notifier:
            self._notifier.stop()
            self._notifier = None

        if self._capture_task:
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
            self._capture_task = None

        self._reader = None

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Decode loop
    # ------------------------------------------------------------------

    async def _decode_loop(self) -> None:
        """Pull frames from the async reader and decode them via cantools."""
        assert self._reader is not None

        async for msg in self._reader:
            if not self._running:
                break

            self._stats.frames_received += 1

            if self._db is None:
                # No DBC loaded — still count frames but can't decode
                continue

            try:
                decoded = self._db.decode_message(
                    msg.arbitration_id,
                    msg.data,
                    decode_choices=True,
                )
                db_msg = self._db.get_message_by_frame_id(msg.arbitration_id)
                self._stats.frames_decoded += 1
                ts = msg.timestamp or time.monotonic()

                for sig_name, value in decoded.items():
                    sig_obj = db_msg.get_signal_by_name(sig_name)
                    sample = SignalSample(
                        timestamp=ts,
                        message_name=db_msg.name,
                        signal_name=sig_name,
                        raw_value=value,
                        unit=sig_obj.unit if sig_obj else None,
                    )
                    self._latest[sig_name] = sample
                    if sig_name not in self._history:
                        self._history[sig_name] = collections.deque(
                            maxlen=_HISTORY_DEPTH
                        )
                    self._history[sig_name].append(sample)

            except cantools.database.UnknownMessageId:
                self._stats.frames_unknown += 1
                self._stats.unknown_ids.add(msg.arbitration_id)
            except Exception:
                # Malformed frame — skip silently
                pass

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def signal_snapshot(self) -> list[dict[str, Any]]:
        """Return the most recent decoded value of every known signal.

        This is the primary multi-module view: one entry per signal from
        every module currently broadcasting on the bus.
        """
        results = []
        for sig_name, sample in sorted(self._latest.items()):
            results.append({
                "signal": sig_name,
                "message": sample.message_name,
                "value": sample.raw_value,
                "unit": sample.unit,
                "timestamp": sample.timestamp,
                "age_ms": round((time.monotonic() - sample.timestamp) * 1000, 1),
            })
        return results

    def signal_history(
        self,
        signal_names: list[str],
        last_n_seconds: float | None = None,
        max_samples: int = 500,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return time-series data for one or more signals.

        Args:
            signal_names: Signal names to retrieve (case-sensitive, from DBC).
            last_n_seconds: If set, only return samples from this many seconds ago.
            max_samples: Cap per signal to avoid overwhelming Claude's context.

        Returns:
            Dict mapping signal_name → list of {timestamp, value, unit}.
        """
        cutoff = (time.monotonic() - last_n_seconds) if last_n_seconds else 0.0
        result: dict[str, list[dict[str, Any]]] = {}

        for name in signal_names:
            deque = self._history.get(name)
            if deque is None:
                result[name] = []
                continue

            samples = [
                {"timestamp": s.timestamp, "value": s.raw_value, "unit": s.unit}
                for s in deque
                if s.timestamp >= cutoff
            ]
            # Honour max_samples by evenly decimating
            if len(samples) > max_samples:
                step = len(samples) / max_samples
                samples = [samples[int(i * step)] for i in range(max_samples)]

            result[name] = samples

        return result

    def correlate(
        self,
        signal_names: list[str],
        last_n_seconds: float | None = 30.0,
        resolution_ms: int = 100,
    ) -> dict[str, Any]:
        """Align multiple signals onto a common time grid for cross-module analysis.

        Useful for asking: "Did the HVAC A/C Permission drop before or after the
        ECM A/C Pressure reading changed?"

        Args:
            signal_names: Signals to correlate (can be from different modules/messages).
            last_n_seconds: Time window (default 30s).
            resolution_ms: Grid resolution in milliseconds (default 100ms).

        Returns:
            {
              "time_axis": [t0, t1, ...],         # seconds relative to window start
              "signals": {
                "SignalName": [v0, v1, ...],       # None where no sample yet
              },
              "events": [                          # detected value-change events
                {"signal": ..., "t_relative": ..., "old": ..., "new": ...},
              ]
            }
        """
        if not signal_names:
            return {"time_axis": [], "signals": {}, "events": []}

        cutoff = (time.monotonic() - last_n_seconds) if last_n_seconds else 0.0
        now = time.monotonic()
        step = resolution_ms / 1000.0

        # Build time axis
        n_steps = int((now - cutoff) / step) + 1
        time_axis = [round(cutoff + i * step - cutoff, 3) for i in range(n_steps)]

        aligned: dict[str, list[Any]] = {name: [None] * n_steps for name in signal_names}
        events: list[dict[str, Any]] = []

        for name in signal_names:
            deque = self._history.get(name)
            if not deque:
                continue

            samples = [s for s in deque if s.timestamp >= cutoff]
            if not samples:
                continue

            # Forward-fill onto grid
            prev_value = None
            sample_iter = iter(samples)
            current_sample = next(sample_iter, None)

            for i, t_abs in enumerate(
                cutoff + i * step for i in range(n_steps)
            ):
                # Advance sample pointer to this time slot
                while current_sample and current_sample.timestamp <= t_abs:
                    next_sample = next(sample_iter, None)
                    if (
                        prev_value is not None
                        and current_sample.raw_value != prev_value
                    ):
                        events.append({
                            "signal": name,
                            "t_relative": round(current_sample.timestamp - cutoff, 3),
                            "old": prev_value,
                            "new": current_sample.raw_value,
                        })
                    prev_value = current_sample.raw_value
                    current_sample = next_sample

                aligned[name][i] = prev_value

        # Sort events by time
        events.sort(key=lambda e: e["t_relative"])

        return {
            "window_seconds": last_n_seconds,
            "resolution_ms": resolution_ms,
            "time_axis": time_axis,
            "signals": aligned,
            "events": events,
        }

    def stats(self) -> dict[str, Any]:
        """Return capture statistics."""
        return {
            "running": self._running,
            "elapsed_seconds": round(self._stats.elapsed_seconds, 1),
            "frames_received": self._stats.frames_received,
            "frames_decoded": self._stats.frames_decoded,
            "frames_unknown": self._stats.frames_unknown,
            "unknown_ids": [hex(i) for i in sorted(self._stats.unknown_ids)],
            "signals_tracked": len(self._latest),
            "dbc_loaded": self._db_path,
        }

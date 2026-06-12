"""Session-scoped diagnostic log. One timestamped file per process run.

The deep-diagnostics stream (the other two are console and the JSONL
event log). Every layer writes here through its own source-tagged
logger; every CAN byte exchange lands here as hex with timing.

Log format (plain text, one entry per line):
    2026-06-11T14:22:01.123  [DAEMON   ]  STATE  from='SCANNING' to='HOLDING'
    2026-06-11T14:22:05.001  [J2534    ]  CAN_EXCHANGE  tx=0x7df  sent='0100'  replies=2  recv='...'  elapsed_ms=23.4

Usage:
    from backprobe import session_log

    log = session_log.get_logger("DAEMON")     # once per layer
    log.event("STATE", frm="SCANNING", to="HOLDING")
    log.error("OPEN_FAILED", exc, device="Supergoose")

    session_log.install_crash_handler()        # once, at startup
    session_log.log_system_info()              # once, at startup

Fail loudly in logs, gracefully in behavior: the logger itself never
raises into the caller — a logger that crashes the daemon is worse
than no log line.
"""

from __future__ import annotations

import datetime
import platform
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

# ─── session state (created lazily on first write, or explicitly by init) ─

_lock = threading.Lock()
_log_file: Path | None = None


def init(log_dir: Path | None = None) -> Path:
    """Choose the log location and open this session's file. Idempotent.

    Call early in __main__ to control where logs go; otherwise the first
    write auto-inits to ./logs/ under the current working directory.
    """
    global _log_file
    with _lock:
        if _log_file is None:
            directory = log_dir if log_dir is not None else Path.cwd() / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            _log_file = directory / f"session_{stamp}.log"
    return _log_file


def session_log_path() -> Path:
    """Path of this session's log file (initializing the session if needed)."""
    return init()


def _write(source: str, line: str) -> None:
    """Append one entry. Never raises — a broken logger must not kill the daemon."""
    try:
        path = init()
        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        entry = f"{ts}  [{source:<9}]  {line}\n"
        with _lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(entry)
    except Exception:
        # Last resort: say something on stderr rather than vanish silently.
        try:
            print(f"session_log write failed: {line}", file=sys.stderr)
        except Exception:
            pass


# ─── the per-layer handle ─────────────────────────────────────────────────


class SessionLogger:
    """A source-tagged writer. Each layer holds its own (see get_logger)."""

    def __init__(self, source: str) -> None:
        self.source = source[:9].upper()

    def event(self, event: str, **details: Any) -> None:
        """One named event, with key=value details."""
        tail = "  ".join(f"{k}={v!r}" for k, v in details.items())
        _write(self.source, f"{event}  {tail}" if tail else event)

    def error(self, event: str, exc: BaseException, **details: Any) -> None:
        """An error with its full traceback, indented under the event line."""
        tail = "  ".join(f"{k}={v!r}" for k, v in details.items())
        suffix = f"  {tail}" if tail else ""
        _write(self.source, f"{event}  error={type(exc).__name__}: {exc}{suffix}")
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        for tb_line in tb.rstrip().splitlines():
            _write(self.source, f"  TRACEBACK  {tb_line}")

    def exchange(
        self,
        tx_id: int,
        sent: bytes,
        replies: list[tuple[int, bytes]],
        elapsed_ms: float,
    ) -> None:
        """One CAN request/response exchange, hex in, hex out, with timing.

        replies is [(ecu, payload), ...] — empty list logs as replies=0,
        which is what a silent probe step looks like.
        """
        recv = "|".join(f"0x{ecu:x}:{payload.hex()}" for ecu, payload in replies)
        _write(
            self.source,
            f"CAN_EXCHANGE  tx=0x{tx_id:x}  sent='{sent.hex()}'"
            f"  replies={len(replies)}  recv='{recv}'  elapsed_ms={elapsed_ms:.1f}",
        )

    def exchange_error(
        self, tx_id: int, sent: bytes, exc: BaseException, elapsed_ms: float
    ) -> None:
        """A failed exchange: what was sent, what went wrong, how long it took."""
        _write(
            self.source,
            f"CAN_ERROR  tx=0x{tx_id:x}  sent='{sent.hex()}'"
            f"  error={type(exc).__name__}: {exc}  elapsed_ms={elapsed_ms:.1f}",
        )


def get_logger(source: str) -> SessionLogger:
    """The handle a layer logs through. Source appears on every line it writes."""
    return SessionLogger(source)


# ─── session-start banner ─────────────────────────────────────────────────


def log_system_info() -> None:
    """Environment snapshot. Call once at startup, before anything can fail."""
    log = get_logger("SYSTEM")
    log.event(
        "SESSION_START",
        python=sys.version.split()[0],
        platform=platform.platform(),
        cwd=str(Path.cwd()),
    )
    log.event("ARGV", argv=sys.argv)


# ─── global crash handler ─────────────────────────────────────────────────


def install_crash_handler() -> None:
    """Route unhandled exceptions — main thread and background — into the log.

    Logging only: device teardown on crash is the daemon's job (atexit and
    signal handlers), not the logger's.
    """
    log = get_logger("CRASH")
    original_hook = sys.excepthook

    def _crash_hook(exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _write("CRASH", f"CRASH  {exc_type.__name__}: {exc_value}")
        for line in tb.rstrip().splitlines():
            _write("CRASH", f"  CRASH_TB  {line}")
        original_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _crash_hook

    original_thread_hook = threading.excepthook

    def _thread_crash_hook(args):
        tb = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        _write(
            "CRASH",
            f"THREAD_CRASH  thread={args.thread.name!r}"
            f"  {args.exc_type.__name__}: {args.exc_value}",
        )
        for line in tb.rstrip().splitlines():
            _write("CRASH", f"  THREAD_CRASH_TB  {line}")
        original_thread_hook(args)

    threading.excepthook = _thread_crash_hook

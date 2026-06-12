"""Session-scoped activity logger for the OBD2 AI Tool.

A single log file is created per process invocation under obd2_mcp/logs/ (inside
the package).  Both the GUI and the MCP server import this module.

Log format (plain text, one entry per line):
    2026-06-06T14:22:01.123  [GUI ]  CONNECT_ATTEMPT  transport='j2534'
    2026-06-06T14:22:01.987  [GUI ]  CONNECTED  device_info={…}
    2026-06-06T14:22:05.001  [GUI ]  CAN_EXCHANGE  tx_id='0x7e0'  sent='2203f190'  recv='6203f190…'  elapsed_ms=12.3
    2026-06-06T14:22:05.010  [MCP ]  TOOL_CALL  read_dtcs  args={}

Public API:
    log_event(event, **kwargs)
    log_error(event, exc, **kwargs)          — includes full traceback
    log_raw_exchange(tx_id, rx_id, sent, recv, elapsed_ms)
    log_tool_call(tool_name, args)
    log_tool_result(tool_name, success, summary)
    log_system_info()                        — call once at session start
    instrument_transport(transport)          — wraps send_raw with CAN logging
    install_crash_handler()                  — logs unhandled exceptions/crashes
    set_source(name)
    session_log_path() -> Path
"""

from __future__ import annotations

import datetime
import platform
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

# ── log file location ─────────────────────────────────────────────────────────

_PKG_DIR = Path(__file__).parent
_LOG_DIR = _PKG_DIR / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_SESSION_TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FILE = _LOG_DIR / f"session_{_SESSION_TS}.log"

_lock = threading.Lock()
_source = "GUI " if "gui" in sys.argv[0].lower() or any("gui" in a.lower() for a in sys.argv) else "MCP "


# ── core writer ───────────────────────────────────────────────────────────────

def _write(line: str) -> None:
    ts = datetime.datetime.now().isoformat(timespec="milliseconds")
    entry = f"{ts}  [{_source}]  {line}\n"
    with _lock:
        with _LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(entry)


# ── public helpers ────────────────────────────────────────────────────────────

def set_source(name: str) -> None:
    """Override the source tag ('GUI ' or 'MCP ') written into each line."""
    global _source
    _source = name.ljust(4)[:4]


def log_event(event: str, **kwargs: Any) -> None:
    """Log a named event with optional key=value details."""
    detail = "  ".join(f"{k}={v!r}" for k, v in kwargs.items())
    _write(f"{event}  {detail}" if detail else event)


def log_error(event: str, exc: BaseException, **kwargs: Any) -> None:
    """Log an error with full traceback so failures can be diagnosed offline."""
    detail = "  ".join(f"{k}={v!r}" for k, v in kwargs.items())
    suffix = f"  {detail}" if detail else ""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # Write the one-liner summary first, then indent the traceback underneath
    _write(f"{event}  error={type(exc).__name__}: {exc}{suffix}")
    # Indent each traceback line so it's visually grouped under the event
    for tb_line in tb.rstrip().splitlines():
        _write(f"  TRACEBACK  {tb_line}")


def log_raw_exchange(
    tx_id: int,
    rx_id: int,
    sent: bytes,
    recv: bytes,
    elapsed_ms: float,
) -> None:
    """Log a raw CAN/ISO-TP byte exchange."""
    _write(
        f"CAN_EXCHANGE"
        f"  tx={tx_id:#05x}"
        f"  rx={rx_id:#05x}"
        f"  sent={sent.hex()}"
        f"  recv={recv.hex()}"
        f"  elapsed_ms={elapsed_ms:.1f}"
    )


def log_raw_error(
    tx_id: int,
    rx_id: int,
    sent: bytes,
    exc: BaseException,
    elapsed_ms: float,
) -> None:
    """Log a failed CAN exchange with the bytes that were sent."""
    _write(
        f"CAN_ERROR"
        f"  tx={tx_id:#05x}"
        f"  rx={rx_id:#05x}"
        f"  sent={sent.hex()}"
        f"  error={type(exc).__name__}: {exc}"
        f"  elapsed_ms={elapsed_ms:.1f}"
    )


def log_tool_call(tool_name: str, args: dict[str, Any]) -> None:
    """Log an MCP tool invocation."""
    _write(f"TOOL_CALL  {tool_name}  args={args!r}")


def log_tool_result(tool_name: str, success: bool, summary: str = "") -> None:
    """Log the outcome of an MCP tool call."""
    status = "OK" if success else "ERROR"
    _write(f"TOOL_RESULT  {tool_name}  status={status}  {summary}".rstrip())


def log_system_info() -> None:
    """Log Python version, OS, and command-line at session start.

    Call this once, right after set_source(), so you can tell exactly what
    environment the session ran in when reviewing the log later.
    """
    _write(f"SYSTEM  python={sys.version.split()[0]}"
           f"  platform={platform.platform()}"
           f"  cwd={Path.cwd()!r}")
    _write(f"SYSTEM  argv={sys.argv!r}")


# ── transport instrumentation ─────────────────────────────────────────────────

def instrument_transport(transport: Any) -> None:
    """Wrap transport.send_raw() to log every CAN byte exchange.

    This is the most valuable field-debugging tool: every request/response
    pair at the wire level is recorded with timing.  Negative responses,
    timeouts, and garbled frames all appear here.
    """
    import time as _time

    original_send_raw = transport.send_raw

    async def _logged_send_raw(
        data: bytes,
        tx_id: int,
        rx_id: int,
        timeout: float = 5.0,
    ) -> bytes:
        start = _time.monotonic()
        try:
            response = await original_send_raw(data, tx_id, rx_id, timeout)
            elapsed = (_time.monotonic() - start) * 1000
            log_raw_exchange(tx_id, rx_id, data, response, elapsed)
            return response
        except Exception as exc:
            elapsed = (_time.monotonic() - start) * 1000
            log_raw_error(tx_id, rx_id, data, exc, elapsed)
            raise

    transport.send_raw = _logged_send_raw


# ── global crash handler ──────────────────────────────────────────────────────

def install_crash_handler() -> None:
    """Install a sys.excepthook that writes unhandled exceptions to the log.

    Without this, a crash in a background thread or an unexpected exception
    during startup silently exits the app with nothing in the log.
    """
    original_hook = sys.excepthook

    def _crash_hook(exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _write(f"CRASH  {exc_type.__name__}: {exc_value}")
        for line in tb.rstrip().splitlines():
            _write(f"  CRASH_TB  {line}")
        original_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _crash_hook

    # Also catch exceptions from non-main threads (Python 3.8+)
    original_thread_hook = threading.excepthook

    def _thread_crash_hook(args):
        tb = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback
        ))
        _write(f"THREAD_CRASH  thread={args.thread.name!r}"
               f"  {args.exc_type.__name__}: {args.exc_value}")
        for line in tb.rstrip().splitlines():
            _write(f"  THREAD_CRASH_TB  {line}")
        original_thread_hook(args)

    threading.excepthook = _thread_crash_hook


# ── utility ───────────────────────────────────────────────────────────────────

def session_log_path() -> Path:
    return _LOG_FILE

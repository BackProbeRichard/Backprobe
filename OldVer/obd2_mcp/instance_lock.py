"""Single-instance enforcement for the OBD2 AI Tool.

Binds a TCP socket to a fixed localhost port.  The first process to bind
succeeds and holds the lock for its lifetime.  Any subsequent process (whether
GUI or MCP server) fails the bind and exits with a clear message.

Usage (call early in main()):

    from obd2_mcp.instance_lock import acquire_instance_lock
    acquire_instance_lock()          # raises SystemExit if another instance runs
"""

from __future__ import annotations

import socket
import sys

# Fixed port — only used for the lock, never for network traffic.
_LOCK_PORT = 57832
_lock_socket: socket.socket | None = None


def acquire_instance_lock(silent: bool = False) -> None:
    """Try to acquire the single-instance lock.

    Raises SystemExit(1) if another OBD2 AI Tool process (GUI or MCP) is
    already running.  Call this once at the start of main() before any UI or
    server initialisation.
    """
    global _lock_socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("127.0.0.1", _LOCK_PORT))
        # Don't listen — we just need the bind to succeed.
        # Keep the socket open so no other process can bind to the same port.
        _lock_socket = sock
    except OSError:
        sock.close()
        msg = (
            "OBD2 AI Tool is already running.\n"
            "Only one instance (GUI or MCP server) may run at a time.\n"
            "Close the existing session before starting a new one."
        )
        if not silent:
            _show_error(msg)
        sys.exit(1)


def _show_error(msg: str) -> None:
    """Show the error in a GUI dialog if tkinter is available, else print."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("OBD2 AI Tool — Already Running", msg)
        root.destroy()
    except Exception:
        print(f"ERROR: {msg}", file=sys.stderr)

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
import hmac
import os
import platform
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

# ─── session state (created lazily on first write, or explicitly by init) ─

_lock = threading.Lock()
_log_file: Path | None = None


def init(log_dir: str | Path | None = None) -> Path:
    """Choose the log location and open this session's file. Idempotent.

    Call early in __main__ to control where logs go; otherwise the first
    write auto-inits to ./logs/ under the current working directory.
    """
    global _log_file
    with _lock:
        if _log_file is None:
            directory = Path(log_dir) if log_dir is not None else Path.cwd() / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            _log_file = directory / f"session_{stamp}.log"
            try:
                _log_file.write_text(_HEADER, encoding="utf-8")  # explain partial VINs
            except Exception:
                pass
    return _log_file


def session_log_path() -> Path:
    """Path of this session's log file (initializing the session if needed)."""
    return init()


def _write(source: str, line: str) -> None:
    """Append one entry. Never raises — a broken logger must not kill the daemon."""
    try:
        path = init()
        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        entry = tokenize_vins(f"{ts}  [{source:<9}]  {line}\n")
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


# ─── VIN tokenization (applied to EVERY log line at write time) ───────────
# Logs carry a PARTIAL, still-usable VIN. Positions 1-11 (make / model / year /
# plant — not personally identifying) are kept readable; the serial (positions
# 12-17, which identifies the individual vehicle) is dropped and a stable keyed
# token is appended in its place, e.g.  1FTEW1E44KK<token>. This happens to the
# VIN in plaintext AND in ASCII-in-hex byte dumps (Mode 09 / UDS DID F190).
#
# Same VIN -> same token, so one vehicle is recognizable across logs; the token
# reveals nothing and isn't guessable without our key. The real, complete VIN
# still flows UNREDACTED to the consumer (the data path / API return); only the
# LOG is partial.
#
# Decoding ("with our key"): the runtime is stdlib-only and the standard
# library has no reversible cipher, so we use a keyed hash (HMAC) plus a local,
# git-ignored keyring that maps token -> full VIN. The HMAC key makes tokens
# stable and unforgeable; the keyring lets us recover the true VIN locally.
#
# REVISIT: ROADMAP.md "Later" — re-evaluate this scheme (keyed-hash + keyring
# vs. real symmetric encryption, key/keyring storage, cross-machine sharing)
# once the consumer API and deployment story firm up.
#
# Key:     $BACKPROBE_VIN_KEY (hex) or an auto-generated local file.
# Config:  $BACKPROBE_CONFIG dir (default ~/.config/backprobe).
# CLI:     python -m backprobe.session_log --in-place logs/*.log   (tokenize files)
#          python -m backprobe.session_log --decode <token>        (recover a VIN)

# Written at the top of every session log so a future reader (Claude or human)
# knows the VINs here are partial-but-usable, and how to recover the full one.
_HEADER = (
    "# Backprobe session log.\n"
    "# VINs below are PARTIAL but usable: positions 1-11 (make/model/year/plant)\n"
    "# are shown; the serial (12-17) is replaced by a stable keyed token, e.g.\n"
    "#   1FTEW1E44KK<token>   — same vehicle always yields the same token.\n"
    "# The full VIN is NOT here (it goes intact to the consumer). Recover it with:\n"
    "#   python -m backprobe.session_log --decode <token>\n"
    "#\n"
)

# VIN charset is A-Z0-9 minus I, O, Q; exactly 17 chars, not part of a longer run.
_VIN_RE = re.compile(r"(?<![A-HJ-NPR-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9])")

_CONFIG_DIR = Path(
    os.environ.get("BACKPROBE_CONFIG", Path.home() / ".config" / "backprobe")
)
_KEYRING_PATH = _CONFIG_DIR / "vin_keyring.tsv"
_key_cache: bytes | None = None
_known_tokens: set[str] | None = None
_keyring_lock = threading.Lock()


def _vin_key() -> bytes:
    """The secret that keys VIN tokens. From $BACKPROBE_VIN_KEY (hex), else an
    auto-generated local file. Stable so tokens match across logs; share it
    (env var) to match a vehicle across machines."""
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    env = os.environ.get("BACKPROBE_VIN_KEY")
    if env:
        _key_cache = bytes.fromhex(env)
        return _key_cache
    key_path = _CONFIG_DIR / "vin_key"
    try:
        _key_cache = bytes.fromhex(key_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        _key_cache = os.urandom(32)
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        key_path.write_text(_key_cache.hex())
        try:
            key_path.chmod(0o600)
        except OSError:
            pass
    return _key_cache


def vin_token(vin: str) -> str:
    """Stable, keyed, opaque token for a VIN. Same VIN -> same token."""
    return hmac.new(_vin_key(), vin.encode("utf-8"), "sha256").hexdigest()[:16]


def _remember(token: str, vin: str) -> None:
    """Record token -> VIN in the local keyring so it can be decoded later.
    Best-effort: never raises into the caller."""
    global _known_tokens
    try:
        with _keyring_lock:
            if _known_tokens is None:
                _known_tokens = set()
                if _KEYRING_PATH.exists():
                    for ln in _KEYRING_PATH.read_text(encoding="utf-8").splitlines():
                        if ln:
                            _known_tokens.add(ln.split("\t", 1)[0])
            if token in _known_tokens:
                return
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with _KEYRING_PATH.open("a", encoding="utf-8") as fh:
                fh.write(f"{token}\t{vin}\n")
            try:
                _KEYRING_PATH.chmod(0o600)
            except OSError:
                pass
            _known_tokens.add(token)
    except Exception:
        pass


def tokenize_vins(text: str) -> str:
    """Replace every VIN in text with its token — plaintext and ASCII-in-hex.
    Records each token->VIN in the keyring. Used at write time on every line."""
    vins: set[str] = set(_VIN_RE.findall(text))
    for run in re.findall(r"[0-9a-fA-F]{34,}", text):
        run = run if len(run) % 2 == 0 else run[:-1]
        try:
            decoded = bytes.fromhex(run).decode("latin-1")
        except ValueError:
            continue
        vins.update(_VIN_RE.findall(decoded))

    for vin in sorted(vins, key=len, reverse=True):
        tok = vin_token(vin)
        _remember(tok, vin)
        red = vin[:11] + tok  # keep make/model/year/plant; serial -> keyed token
        text = text.replace(vin, red)
        text = text.replace(vin.encode().hex(), red.encode().hex())
        text = text.replace(vin.encode().hex().upper(), red.encode().hex().upper())
    return text


def decode_token(token: str) -> str | None:
    """Recover the VIN for a token from the local keyring, or None if unknown."""
    try:
        for ln in _KEYRING_PATH.read_text(encoding="utf-8").splitlines():
            tok, _, vin = ln.partition("\t")
            if tok == token:
                return vin
    except FileNotFoundError:
        pass
    return None


def tokenize_file(path: str | Path, in_place: bool = False) -> tuple[Path, bool]:
    """Tokenize VINs in an existing log file (e.g. legacy/external logs).
    Returns (output_path, changed?). Without in_place, writes alongside as
    <name>.redacted<suffix> and leaves the original."""
    p = Path(path)
    original = p.read_text(encoding="utf-8", errors="replace")
    redacted = tokenize_vins(original)
    out = p if in_place else p.with_suffix(".redacted" + p.suffix)
    out.write_text(redacted, encoding="utf-8")
    return out, original != redacted


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m backprobe.session_log",
        description="Tokenize VINs in session logs, or decode a token to its VIN.",
    )
    ap.add_argument("files", nargs="*", help="log files to tokenize")
    ap.add_argument("--in-place", action="store_true", help="overwrite the file")
    ap.add_argument("--decode", metavar="TOKEN", help="print the VIN for a token")
    args = ap.parse_args(argv)
    if args.decode:
        vin = decode_token(args.decode)
        print(vin if vin else f"unknown token: {args.decode}")
        return 0 if vin else 1
    for f in args.files:
        out, changed = tokenize_file(f, in_place=args.in_place)
        print(f"{'tokenized' if changed else 'no VINs found'}: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

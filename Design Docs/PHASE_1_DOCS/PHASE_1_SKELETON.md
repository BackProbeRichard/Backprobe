# Phase 1 Skeleton — Package Structure & Transport Interface

*v1 — 2026-06-11. Child of PHASE_1_SCOPE.md. Defines the package layout, the
dependency posture, and the transport interface that both the real J2534
backend and the virtual backend implement. This is the foundation the rest of
Phase 1 is built on.*

---

## Package Name

`backprobe` — the importable Python package. (OldVer used `obd2_mcp`, which was
MCP-centric; the core is no longer MCP-first, so the name drops it.)

Invoked as: `python -m backprobe`

---

## Directory Layout

```
Backprobe/                       repo working directory (already exists)
└── backprobe/                   the package
    ├── __init__.py
    ├── __main__.py              entry point: python -m backprobe
    │
    ├── transport/               TIER 1 — move bytes, nothing more
    │   ├── __init__.py
    │   ├── base.py              the interface: ABCs + dataclasses (below)
    │   ├── constants.py         J2534 numeric constants (validated vs from-spec)
    │   ├── j2534.py             real backend — Windows + ctypes + DLL
    │   └── virtual.py           fake backend — pure Python, synthesizes a vehicle
    │
    ├── obd2/                    OBD2 decode — pure functions, bytes → meaning
    │   ├── __init__.py
    │   ├── decode.py            PID parse, VIN, Mode 06, DTC, bitmask walk
    │   └── pids.py              PID name/unit table (the 17 we can decode)
    │
    ├── daemon/                  the Watcher — policy, orchestration
    │   ├── __init__.py
    │   ├── watcher.py           the state machine (SCANNING→HOLDING→…)
    │   ├── interrogate.py       probe + census + harvest (drives transport verbs)
    │   └── events.py            JSON-lines connection-event log writer
    │
    └── session_log.py           diagnostic session log (CLAUDE.md standard)
```

Why this shape:
- **`transport/` only moves bytes.** No probe logic, no decode. It exposes the
  verbs; the daemon decides when to call them.
- **`obd2/` is pure functions.** `decode.py` takes bytes and returns meaning —
  no I/O, no device. Trivially testable on any machine, no hardware.
- **`daemon/` is policy.** The probe *sequence*, the census *window*, the poll
  *cadence*, the teardown *guarantee* — all here, driving transport verbs.
- **`session_log.py` at root**, not in a `logging/` folder — a `logging` package
  would shadow the stdlib module. (Lesson taken from naming hygiene, not OldVer.)

`interrogate.py` is split from `watcher.py` so the state machine stays readable
and the harvest is testable alone. Easy to merge later if it feels thin.

---

## Dependency Posture

**Phase 1 target: zero third-party runtime dependencies.** Standard library +
`ctypes` only.

- The J2534 backend needs `ctypes` (stdlib) and `winreg` (stdlib, Windows).
- The virtual backend, decode, daemon, events, and session log are pure stdlib.
- No `pydantic` (OldVer used it for settings — Phase 1 config is small enough
  for a plain dataclass or module-level constants).
- No `python-obd`, no `python-can` — those were the ELM327/SocketCAN paths.

This is a property worth protecting: a diagnostic core that depends on nothing
is easy to ship to a locked-down test bench and easy to audit. Dev tooling
(ruff, mypy, pytest) is fine as *dev* dependencies; runtime stays clean.

---

## Runtime Targets

- **Python 3.11+** everywhere.
- **On Windows (the bench): 32-bit Python**, because J2534 DLLs are 32-bit
  stdcall. This is a hard runtime requirement for the J2534 backend only.
- **On Linux (the dev box): any Python.** Only the virtual backend runs here;
  the J2534 backend imports `winreg`/`WinDLL` lazily so importing the package
  on Linux never fails.

---

## Concurrency

**Phase 1 is synchronous.** One device, one channel serialized anyway, a poll
loop with signal handlers. Async buys nothing here and adds weight. The
"multiple devices at once" future is a threads-or-async decision for when a
second device actually arrives — explicitly deferred, not designed against.

(OldVer was async because it parallelized PID queries with `gather` — but those
serialize on the single channel's I/O lock regardless, so the parallelism was
cosmetic. We drop it.)

---

## The Transport Interface (`transport/base.py`)

The verbs from PHASE_1_SCOPE.md, as abstract types. Both `j2534.py` and
`virtual.py` implement these. The daemon speaks only this vocabulary — it never
sees a J2534 call. Shapes shown Python-style; names are what matter.

```python
# ─── Data carriers (immutable) ───────────────────────────────────────────

@dataclass(frozen=True)
class Device:
    """One discovered device. Returned by enumerate(), passed to open()."""
    vendor: str
    name: str
    address: str          # how the backend reaches it (J2534: DLL path)

@dataclass(frozen=True)
class DeviceReport:
    """Identity card, captured at open."""
    vendor: str
    name: str
    address: str
    firmware: str | None
    dll_version: str | None
    api_version: str | None

@dataclass(frozen=True)
class ConnectProfile:
    """One connection attempt's parameters. The probe is a loop over these."""
    protocol: str         # Phase 1: "ISO15765"
    bitrate: int          # 500000 | 250000
    addressing: str       # "11-bit" | "29-bit"

@dataclass(frozen=True)
class Reply:
    """One ECU's answer to a request."""
    ecu: int              # source address, e.g. 0x7E8
    payload: bytes        # response bytes, CAN-ID prefix already stripped


# ─── The three behavioral objects ────────────────────────────────────────

class TransportBackend(ABC):
    """A KIND of transport. Phase 1 has two: J2534Backend, VirtualBackend."""

    @abstractmethod
    def enumerate(self) -> list[Device]:
        """Discover devices of this kind. No hardware touched. Never raises —
        returns [] if none; skips and logs dead entries."""

    @abstractmethod
    def open(self, device: Device) -> "Session":
        """Take exclusive ownership of one device. One attempt, no waiting.
        Raises DeviceBusy | DeviceLost | InternalError."""


class Session(ABC):
    """One device, opened and held by us."""

    @abstractmethod
    def info(self) -> DeviceReport: ...

    @abstractmethod
    def read_voltage(self) -> int:
        """Pin-16 battery voltage, in millivolts. The vehicle-presence signal.
        Raises DeviceLost | NotSupported."""

    @abstractmethod
    def connect(self, profile: ConnectProfile) -> "Channel":
        """Attempt a vehicle connection with exactly one profile. One attempt.
        Raises ConnectError | DeviceLost."""

    @abstractmethod
    def close(self) -> None:
        """Release the device, completely and unconditionally. Idempotent.
        Never raises — failures are logged and swallowed. After close, another
        program's open must succeed immediately."""


class Channel(ABC):
    """A live protocol connection to a vehicle through a held device."""

    @abstractmethod
    def ask(self, request: bytes, timeout: float) -> Reply:
        """Send one request, return the first matching reply. Echoes, first-frame
        markers, and stale replies skipped internally. Raises Timeout | DeviceLost."""

    @abstractmethod
    def ask_all(self, request: bytes, window: float) -> list[Reply]:
        """Send one functional request, collect every ECU that answers within
        the window. The census verb. [] is a valid answer. Raises DeviceLost."""

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down this protocol connection, keep the device held. Idempotent."""
```

### Errors (`transport/base.py`)

The Phase 1 taxonomy from PHASE_1_SCOPE.md, as exception classes. These are the
*only* exceptions the transport may raise; raw J2534 return codes never escape.

```python
class TransportError(Exception): ...          # base
class DeviceBusy(TransportError): ...          # another program owns it
class DeviceLost(TransportError): ...          # unplugged / powered off
class ConnectError(TransportError): ...        # a profile didn't take
class Timeout(TransportError): ...             # no reply in time
class NotSupported(TransportError): ...        # device/vehicle lacks feature
class InternalError(TransportError): ...       # our bug / broken DLL
```

---

## Build Order

1. **`transport/base.py`** — the interface above. Nothing works without it;
   both backends and the daemon import it. Pure types, no logic.
2. **`transport/constants.py`** — J2534 numbers, sourced from the standard,
   cross-checked against OldVer, each tagged validated-on-hardware vs from-spec.
3. **`session_log.py`** — port the OldVer logging concept (per CLAUDE.md). The
   daemon and backends log through it from line one.
4. **`obd2/decode.py` + `obd2/pids.py`** — pure decode functions. Testable
   immediately, no hardware.
5. **`transport/virtual.py`** — the fake backend. First *runnable* milestone:
   exercises the whole interface on the Linux dev box.
6. **`daemon/`** — events writer, interrogate (probe/census/harvest), watcher
   (state machine). Runs end-to-end against the virtual backend on Linux.
7. **`transport/j2534.py`** — the real backend. Written here, validated on the
   Windows bench. By now everything above it is proven against the virtual one,
   so a bench failure points squarely at the ctypes/DLL layer.

Phase 1 is "done" (per scope) when step 7 passes on a real vehicle and the
device releases absolutely on every exit path.

---

## Decisions To Confirm

1. **Package name `backprobe`** — confirmed.
2. **Zero runtime deps** — confirmed. Standard library + ctypes only; no third-party runtime packages.
3. **Synchronous Phase 1** — confirmed. Phase 1 is synchronous; async is explicitly
   deferred. The compelling future case is not multiple devices but multiple channels
   on a single Session: HS-CAN + MS-CAN + LIN simultaneously through one Supergoose.
   Whether the DLL handles channels independently or serializes them internally is a
   bench question — session log timestamps will answer it. No design work needed now;
   the Channel interface does not fight an async migration when the time comes.
4. **`interrogate.py` split from `watcher.py`** — confirmed split. Keep separate.

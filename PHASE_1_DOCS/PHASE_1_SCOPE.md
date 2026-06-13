# Phase 1 Scope — The Watcher

*v1 — 2026-06-09. Child of ROADMAP.md. Defines what Phase 1 builds, the objects
it is built from, and the exact commands each object answers to. If a command
is not defined here, Phase 1 does not call it.*

---

## What Phase 1 Delivers (summary)

A terminal daemon for the Windows test bench. It finds J2534 devices, holds
one open, watches battery voltage for a vehicle to be plugged in, probes the
CAN flavor, harvests an identity snapshot (VIN, ECU census, supported PIDs,
MIL/DTC count, Mode 06, voltage), writes one JSON-lines event per step, and
releases the device instantly and absolutely on every exit path.

Out of scope for Phase 1: any consumer API surface (Phase 2), GUI/MCP
(Phase 3), K-line/J1850, UDS, Mode 22, flashing, multi-device scheduling,
SocketCAN. The seams for those exist; the code does not.

---

## The Object Model

Four objects, owned top-down. Each line is a "has" relationship:

```
Daemon                    the program. Owns all policy and the event log.
 └─ TransportBackend      a KIND of transport (Phase 1: J2534 only).
     └─ Device            one physical box the backend found (Supergoose).
         └─ Session       that box, opened and exclusively held by us.
             └─ Channel   a live protocol connection to a vehicle through it.
```

**Policy vs mechanism rule:** the Daemon decides *when and why* (poll cadence,
probe order, retry on busy, when to give up). The objects below it know only
*how* (the J2534 calls). The Daemon never issues a J2534-specific call; it
only speaks the verbs defined below. This is the seam that lets a second
backend (SocketCAN, later) drop in without touching daemon logic — and the
seam a future flashing plugin borrows via the Session.

A note on plurality: the Daemon holds a *list* of backends and a *list* of
sessions. In Phase 1 both lists have one element. The lists are the design;
the single element is the build.

---

## Command Definitions

Format per command: what it does, what you give it, what you get back, what
can go wrong. "Under the hood" names the J2534 mechanism so the abstraction
map can be cross-referenced; daemon code never sees that level.

### TransportBackend

**`backend.enumerate() → list[Device]`**
- Scan the machine for installed devices of this transport kind. Touches no
  hardware — discovery only. Safe to call repeatedly (the Daemon calls it on
  a cadence to notice newly plugged-in boxes).
- Give: nothing.
- Get: a list of Device records — vendor, product name, and how to reach it
  (for J2534: the DLL path from the registry). Empty list = nothing installed.
- Errors: none. A backend that finds nothing returns `[]`; a dead registry
  entry (DLL file missing) is skipped and logged, never raised.
- Under the hood: walk `HKLM\SOFTWARE\PassThruSupport.04.04` (+ WOW6432Node),
  read `Name`, `Vendor`, `FunctionLibrary`; skip entries whose DLL is absent.

**`backend.open(device) → Session`**
- Take exclusive ownership of one physical device. One attempt — no waiting.
  The "wait politely until the OEM tool lets go" loop is Daemon policy built
  by calling this repeatedly.
- Give: one Device from `enumerate()`.
- Get: a Session — the held device.
- Errors: `DeviceBusy` (another program owns it — wiTECH, FDRS, etc.),
  `DeviceLost` (unplugged/powered down between enumerate and open),
  `InternalError` (DLL loaded but broken).
- Under the hood: `LoadLibrary` + bind the `PassThru*` exports, then
  `PassThruOpen`. Version info is captured immediately (`PassThruReadVersion`)
  so it is in the log even if everything after fails.

### Session

**`session.info() → DeviceReport`**
- Identity card of the held device, captured at open.
- Get: vendor, name, DLL path, firmware version, DLL version, API version.
- Errors: none (data already captured at open).

**`session.read_voltage() → millivolts`**
- Read pin-16 battery voltage right now. This is the vehicle-presence signal:
  ~0 mV = cable dangling; ~12,000+ mV = plugged into a vehicle. The Daemon
  polls this on a cadence; the Session just answers.
- Give: nothing.
- Get: integer millivolts.
- Errors: `DeviceLost` (box unplugged), `NotSupported` (device can't read
  voltage — logged once, Daemon falls back to probe-based detection).
- Under the hood: `PassThruIoctl(READ_VBATT)`.

**`session.connect(profile) → Channel`**
- Attempt a vehicle connection using exactly one connection profile. One
  attempt, one profile — the probe *sequence* is Daemon policy (a loop over
  profiles), not Session magic.
- Give: a ConnectProfile: `{protocol: "ISO15765", bitrate: 500000 | 250000,
  addressing: "11-bit" | "29-bit"}`. (Phase 1 allows only these values.)
- Get: a Channel, armed and ready to exchange bytes (filters installed,
  loopback off).
- Errors: `ConnectError` (this profile didn't take — Daemon advances the
  probe), `DeviceLost`.
- Under the hood: `PassThruConnect` at the profile's bitrate/flags;
  `SET_CONFIG` loopback off; flow-control filter pairs installed for the
  full physical-response range (11-bit: pattern 0x7E8–0x7EF paired with
  flowctrl 0x7E0–0x7E7; 29-bit equivalents) so multi-ECU vehicles are heard.

**`session.close() → nothing`**
- Release the device, completely and unconditionally. Idempotent — safe to
  call twice, safe to call when nothing is connected. After close, another
  program's open must succeed immediately.
- Errors: none, ever. Failures inside are logged and swallowed; close always
  finishes.
- Under the hood, in sacred order: stop periodic messages → clear all
  filters → `PassThruDisconnect` (if a channel is up) → `PassThruClose`.

### Channel

**`channel.ask(request, timeout) → Reply`**
- Send one request and return the first matching reply. Used for
  single-answer queries (VIN to a specific ECU, voltage-gated probes).
- Give: request bytes (e.g. `01 00`), timeout in seconds.
- Get: one Reply: `{ecu: source address, payload: bytes}`. TX echoes,
  ISO15765 first-frame markers, and stale replies from earlier timed-out
  requests are skipped internally — the Daemon never sees them.
- Errors: `Timeout` (no matching answer), `DeviceLost`.
- Under the hood: `PassThruWriteMsgs` + `PassThruReadMsgs` loop under the
  channel's I/O lock (one request/response in flight per channel, ever);
  skip `TX_MSG_TYPE` and `START_OF_MESSAGE` frames; match reply SID/PID.

**`channel.ask_all(request, window) → list[Reply]`**
- Send one functional (broadcast) request and collect *every* ECU that
  answers within the window. This is the census verb: `01 00` to 0x7DF and
  the engine, transmission, and anyone else all reply.
- Give: request bytes, collection window in seconds (default 0.5).
- Get: list of Reply, one per responding ECU, in arrival order. Empty list
  is a valid answer (and is how a failed probe step looks).
- Errors: `DeviceLost`. (No answers is data, not an error.)

**`channel.disconnect() → nothing`**
- Tear down this protocol connection but keep the device held. Used between
  vehicles: voltage drops → disconnect channel → keep session, keep
  watching. Idempotent.
- Under the hood: clear this channel's filters → `PassThruDisconnect`.

### Every byte logged

Every `ask`/`ask_all` automatically writes a CAN_EXCHANGE log line — request
hex, reply hex, elapsed ms — per the CLAUDE.md logging standard
(`instrument_transport` pattern). This is not optional per call; it is wired
into the Channel itself.

---

## Daemon Policy (summary — the loop the objects serve)

States, with the commands each one uses:

1. **SCANNING** — no device held. `enumerate()` every few seconds until a
   Device appears, then `open()` it. `DeviceBusy` → log once, retry politely
   until the OEM tool releases it.
2. **HOLDING** — device held, no vehicle. `read_voltage()` on a cadence
   (~2 s). Voltage ≥ threshold (~6 V) → INTERROGATING.
3. **INTERROGATING** — run the probe: for each profile in
   `[11-bit/500k, 29-bit/500k, 11-bit/250k, 29-bit/250k]` → `connect(profile)`,
   `ask_all("01 00")`; any reply wins. Then harvest through the winning
   Channel: VIN (Mode 09), ECU census + supported PIDs (bitmask walk), MIL +
   DTC count (Mode 01 PID 01), Mode 06 results, voltage. Log each step;
   finish with one `connection_complete` record. → ATTACHED.
4. **ATTACHED** — vehicle on the line, snapshot done. Keep polling voltage.
   Drop below threshold → log disconnect, `channel.disconnect()`, → HOLDING.
5. **EXIT (any path: Ctrl-C, crash, kill)** — `close()` every held session.
   Wired into the normal path, the crash handler, `atexit`, and the signal
   handlers. This is Hard Rule #2 and it is the Phase 1 acceptance test.

Event log: JSON lines, one object per event, per the agreed mockup
(`PHASE_1_LOG_MOCKUP.jsonl`). Session log: per CLAUDE.md standard.

---

## Error Taxonomy (Phase 1 subset)

The only errors any object may raise. Raw J2534 return codes never escape;
every non-zero return gets `PassThruGetLastError` text attached in the log.

| Error | Meaning | Daemon's response |
|---|---|---|
| `DeviceBusy` | Another program owns the device | Log once, retry politely |
| `DeviceLost` | Device unplugged / powered off | Close session, → SCANNING |
| `ConnectError` | This profile didn't take | Advance the probe |
| `Timeout` | No reply to this request | Log it, continue harvest |
| `NotSupported` | Device/vehicle lacks the feature | Log once, degrade |
| `InternalError` | Our bug or broken DLL | Log loudly, close, → SCANNING |

---

## Done When (from ROADMAP.md, restated as tests)

1. Daemon runs unattended on the bench; plugging the Supergoose into any
   2008+ vehicle produces a correct `connection_complete` log entry within
   seconds, no typing.
2. Ctrl-C, crash, and forced kill all release the device so completely that
   an OEM application opens it immediately afterward — tested deliberately.
3. A multi-ECU vehicle shows every responding ECU in the census, not just
   the engine.
4. Every byte exchanged with the vehicle appears in the session log as hex
   with timing.

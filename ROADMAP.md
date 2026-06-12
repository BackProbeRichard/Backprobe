# Backprobe — Canon Roadmap

*v1 — 2026-06-09. This file is canon. It supersedes the prototype documents in
`J2534 Reference Docs/Fable 5 Files/` (charter + abstraction map), which remain
useful as technical reference but are not authoritative. Where this file and
CLAUDE.md conflict with anything else, this file and CLAUDE.md win.*

---

## The Product

The core: a program that owns the J2534 device, talks to the vehicle, and offers
a simple, protocol-free way to get vehicle data. Consumers (GUI, MCP, terminal,
home automation, anything) sit on top and never touch transport or protocol
mechanics.

## Layer Model

```
TIER 3 — Consumer API        vehicle.read_dtcs(), .stream()   ← what apps call
TIER 2 — Query Engine        query(), probe, scheduler,        ← request/response,
                             decode tables, error mapping        multi-frame, NRC retry
TIER 1 — Transport Wrapper   open/connect/send/receive/        ← ergonomic J2534 wrapper
                             filters/periodics/options
TIER 0 — Vendor DLL          PassThru* exports (J2534 04.04)   ← Supergoose Plus et al.
```

Tier boundaries are hard. Tier 3 consumers never see J2534, PIDs, or NRCs.
Tier 1 is the seam future transports plug into.

## v1 Scope

- **CAN only** (ISO 15765-4): 11-bit/500k → 29-bit/500k → 250k probe order.
  Probe list designed extensible; K-line/J1850 are probe steps we add later,
  not architecture changes.
- **Generic OBD2 only**: SAE J1979 Modes 01–0A. (0A = permanent DTCs — required
  to correctly report what survives a code clear.)
- **Windows 10/11, 32-bit Python** runtime; Linux development.
- **Validated device:** Opus IVS Supergoose Plus.

## Hard Rules (every phase)

1. **The core owns the device while running.** Open once, hold. If another app
   owns it, wait politely and take over when freed.
2. **Release is instant and absolute.** Every exit path — Ctrl-C, crash, kill —
   releases the device within seconds. Teardown order is sacred: stop periodics
   → clear filters → disconnect → close. A wedged device breaks the next
   flash on the shop laptop.
3. **The consumer never sees a protocol.** Named parameters, scaled values with
   units, plain-English errors from a fixed taxonomy (DeviceBusy, DeviceLost,
   ConnectError, NotSupported, ConditionsNotCorrect, Timeout, InternalError).
4. **Re-learn the vehicle on every connect.** A flash can change everything.
   Only the VIN→protocol shortcut may persist, and it falls back to full probe.
5. **Session logging built into every module** per the CLAUDE.md standard.

---

## Phase 1 — The Watcher (terminal daemon)

A daemon that runs unattended on the test bench:

1. Watch the Windows registry for J2534 devices.
2. Open and hold the device; poll battery voltage to detect vehicle plug-in.
3. On vehicle detected: auto-probe (CAN variants), then pull VIN, winning
   protocol, ECU census, supported-PID map, MIL status + DTC count, Mode 06, voltage.
4. Write one JSON-lines record per connection event. Log disconnect on
   voltage drop. Loop forever; exit clean on Ctrl-C.

**Done when:** plugging the Supergoose into any 2008+ vehicle produces a correct
log entry with no typing, and stopping the daemon — including forced kill —
releases the device so completely that an OEM tool opens it immediately after.

**Why first:** forces every risky piece (DLL loading, device exclusivity, probe,
real traffic, real vehicles) through end-to-end testing before any API exists.
Tests run on the work-laptop bench; everything else develops at home.

## Phase 2 — The API (promote the internals)

Freeze Phase 1's machinery behind the published consumer surface:

- `list_devices` / `open` / `connect` / `read` / `read_many` / `stream` /
  `available_params` / `mil_status` / `readiness` / `read_dtcs` / `clear_dtcs` /
  `test_results` / `identity` / `request` — Modes 01–0A complete.
- Errors normalized to the taxonomy. NRC 0x78 retried internally, never surfaced.
- Two escape hatches, clearly fenced: `request()` (raw mode/data through the
  managed session) and `session.transport` (Tier 1 access — the seam for
  future UDS/flashing work).
- Tests against a fake J2534 DLL (pure software — runs on the Linux dev box);
  validation passes on the Supergoose against real vehicles.
- Written API documentation a stranger could build from.

**Done when:** a demo script written against only the docs can connect, read
codes, and stream live data — and the Phase 1 daemon itself runs on the public
API as its first honest consumer.

## Phase 3 — Test Harness Consumers (GUI + MCP)

- **GUI scan tool**: codes (stored/pending/permanent + freeze frames), clear
  with verification, live data, readiness, vehicle identity.
- **MCP server**: the same capabilities exposed as tools for AI agents.

**The honesty rule:** both consume only the public Phase 2 API — the exact same
calls any outside consumer gets. If building either requires reaching into the
core's guts, that is an API defect and the API gets fixed. These are acceptance
tests, not products.

**Done when:** a tech who has never seen this project can plug in, read and
clear codes, and watch live data without instructions — and an AI agent can do
the same through MCP.

---

## Later (designed-for now, built later)

- **Additional transports** — new Tier 1 implementations (SocketCAN/Canable,
  ELM327) behind the same Tier 2/3 surface. Architecture must make this a
  new module, never a rewrite.
- **Async / concurrency** — Phase 1 is synchronous. Two future cases both remain
  open: multiple networks simultaneously through a single Session (HS-CAN, MS-CAN,
  LIN on one Supergoose), and multiple devices simultaneously (multiple Sessions).
  Before committing to a concurrency model (threads vs async), bench-test whether
  the DLL handles channels independently or serializes them internally — session log
  timestamps will answer it. The transport interface does not block either path.
- **K-line / J1850** — new probe steps in the existing probe list.
- **Plugins folder** — drop-in parameter packs (data, not code) that every
  consumer gains at once; a bad plugin never crashes anything.
- **Manufacturer depth** — Mode 22 data packs first, UDS sessions later, both
  building on the `request()` / `session.transport` escape hatches.
- **Service mode** — the core answering network requests (home automation).
  The data is already plain; this is a wrapper, not a rebuild.

---

## Reference Material

- `J2534 Reference Docs/Fable 5 Files/j2534-abstraction-map (3).md` — J2534
  command mapping, full error-code table, probe details, API shapes. Use it
  when implementing; it is reference, not canon.
- `J2534 Reference Docs/sae.j2534.2002.pdf` — SAE J2534 spec.
- `J2534 Reference Docs/Supergoose Docs/` — vendor boilerplate headers.
- `OldVer/` — v0.1.4 working implementation; read for prior decisions.

# Phase 2 Scope — The API

*v1 — 2026-06-17. Child of ROADMAP.md. Defines what Phase 2 builds and the
decisions that must be made before code is written. Open decisions are marked
explicitly; settled decisions are stated as facts.*

---

## What Phase 2 Delivers

A running daemon that consumers talk to over a defined wire protocol. The consumer
sends commands and receives responses. It never touches transport, protocol, or
device mechanics — those are entirely behind the daemon wall.

The daemon exposes the full OBD2 Modes 01–0A surface as named, scaled, plain-English
commands. Errors are normalized to the taxonomy. NRC 0x78 retries happen internally
and are never surfaced.

Phase 2 also ships:
- A thin Python client library so Python consumers don't write raw socket code
- Written API docs a stranger could build a consumer from without reading the source

---

## Settled Decisions

**Daemon model only.** Backprobe is a server process. Consumers connect over IPC.
No consumer imports backprobe internals directly. The process boundary enforces the
abstraction — consumers cannot accidentally reach below the API.

**Consumer isolation is absolute.** The consumer never sees: J2534, PassThru calls,
CAN addressing mode, OBD2 mode/PID numbers, 32-bit Python, NRC retry logic, or
device exclusivity. They send a named command, they get a named response.

**Daemon is always-on.** It starts, finds a device, waits for a vehicle. Consumers
connect and disconnect at will. The daemon does not start or stop per consumer.

**Phase 1 machinery is unchanged.** The Watcher, probe, interrogation, J2534 transport,
and event log are Phase 1 deliverables. Phase 2 adds the command handler layer on top.
It does not rewrite what already works.

---

## Open Decisions

Each decision below must be made before implementation begins. Options are listed with
tradeoffs; a recommendation is given where one is clear.

---

### Decision 1 — IPC Transport ✓ SETTLED

The transport layer is **pluggable**. The daemon's message handler operates on an
abstract transport interface (deliver a framed message / send a framed message back).
Concrete transport implementations plug in underneath without touching the message
handler. This is the same seam pattern used in Phase 1 for the J2534 backend.

**Phase 2 ships one transport: TCP, loopback-bound.**

- Consumer connects to `127.0.0.1:2534` (configurable — see Decision 6)
- Binding to `127.0.0.1` means the daemon cannot accept connections from outside the
  machine — local-only by default, no firewall rules or authentication needed
- Multiple consumers can connect simultaneously on the same port
- Port conflicts are detected and reported cleanly at daemon startup
- Named pipe, Unix socket, WebSocket, and stdio are valid future transport
  implementations; an advanced user can write their own against the abstract interface
- Network exposure is an explicit opt-in: change the bind address to `0.0.0.0` in
  config. Phase 2 does not ship network-accessible mode.

---

### Decision 2 — Wire Protocol Format ✓ SETTLED

**JSON-RPC 2.0** over newline-delimited framing (`\n` terminates each message).

- Request:  `{"jsonrpc":"2.0","id":1,"method":"read_dtcs","params":{"ecu":"engine"}}`
- Response: `{"jsonrpc":"2.0","id":1,"result":{"dtcs":["P0300","P0301"]}}`
- Push event (stream): `{"jsonrpc":"2.0","method":"stream_data","params":{...}}`

JSON-RPC 2.0 is chosen because:
- Request/response correlation and push notifications are built into the spec
- Every language has an existing client library or can implement the trivial spec from scratch
- Human-readable — debuggable with `nc`, `wscat`, or any text tool
- Verbosity cost is negligible at OBD2 data rates

Newline framing works identically over TCP, named pipe, Unix socket, and stdio —
consistent with the pluggable transport decision.

---

### Decision 3 — Streaming Model ✓ SETTLED

**Single endpoint, two channel types declared on connection via handshake.**

A consumer connects to the daemon's single port and declares which channel type this
connection is via its first message:

```json
{"jsonrpc": "2.0", "id": 1, "method": "open", "params": {"channel": "command"}}
{"jsonrpc": "2.0", "id": 1, "method": "open", "params": {"channel": "stream"}}
```

**Admin channel** — request/response only:
- All named commands: `identity`, `read_dtcs`, `clear_dtcs`, `readiness`, `status`, etc.
- Consumer sends a request, daemon sends exactly one reply, done

**Stream channel** — daemon pushes, consumer only listens:
- Mode 01 live data once subscribed (continuous push at the agreed rate)
- Autonomous events: `vehicle_connected`, `vehicle_disconnected`, `device_lost`, etc.
- Consumer never sends commands on this connection

A multithreaded consumer opens **both connections simultaneously** to the same port —
one thread per channel. A simple consumer that does not need live data opens only the
admin channel. The transport is unaware of the distinction; it accepts connections
and moves bytes. Channel routing happens at the protocol layer after the handshake.

---

### Decision 4 — Multi-Consumer Behaviour ✓ SETTLED

**Admin channel: one connection at a time. Stream channel: unlimited connections.**

- Only one consumer may hold the admin channel. A second consumer attempting to open
  a admin channel while one is already held receives an error and must wait or retry.
  This eliminates write conflicts entirely — there is never ambiguity about who can call
  `clear_dtcs` or any other command.

- Any number of consumers may connect to the stream channel simultaneously. The daemon
  fans out all push events (live data, autonomous events) to every connected stream
  client. A logger, a GUI, and an MCP agent can all receive the same stream at once
  without coordinating with each other.

This creates a natural owner/observer split: one consumer owns the admin channel and
drives the interaction; any number of observers listen on the stream channel.

---

### Decision 5 — Session Model ✓ SETTLED

**Autonomous daemon with explicit consumer session acceptance.**

The daemon connects to vehicles on its own — Phase 1 Watcher behaviour unchanged. The
consumer never sees devices, profiles, or probe sequences. When a consumer connects to
the admin channel, the daemon announces what it has and waits for the consumer to
accept before granting API access:

```
Consumer opens admin channel
    ↓
Daemon → "vehicle_offer": {vin, make, model, year, protocol, mil_on, dtc_count}
    ↓                       (or "no_vehicle" if nothing is connected yet)
Consumer → "vehicle_accept"
    ↓
Consumer has full access to the command API
```

This lets the consumer validate the vehicle before committing — an insurance snapshot
device can check the VIN, a fleet tool can confirm the make. The consumer can also
hold the admin channel open without accepting, waiting for a `vehicle_offer` event
when the next vehicle arrives.

If the vehicle disconnects while a consumer session is active, the daemon sends a
`vehicle_lost` notification and the session ends. The consumer waits for the next
`vehicle_offer`.

**Stream channel subscription model:**

The stream channel does not push anything until the consumer declares what it wants.
The admin channel controls stream subscriptions via `stream_subscribe` /
`stream_unsubscribe`. The daemon only polls what has been subscribed — no unnecessary
bus traffic when no consumer is connected or when subscriptions are narrow.

**Any read command can be subscribed to at a consumer-specified cadence.** The daemon
is not the authority on what is worth monitoring continuously — the consumer is. A few
examples across the full cadence range:

- RPM, throttle position (Mode 01) → every 100ms
- Coolant temp, fuel level (Mode 01) → every 1s
- Stored / pending / permanent DTCs (Mode 03/07/0A) → every 5s
- On-board monitor test results (Mode 06) → every 60s (emissions monitoring use case)
- Readiness monitors (Mode 01 PID 01) → every 30s

The daemon maintains a subscription list: `(command, params, cadence)` tuples. Each
fires on its own timer and pushes results to all connected stream consumers. If two
consumers subscribe to the same command at the same cadence, the daemon polls once and
fans out — no duplicate bus traffic.

Write commands (`clear_dtcs`, `request` with write payloads) cannot be subscribed.

Autonomous events (`vehicle_connected`, `vehicle_lost`, `device_lost`) are always
pushed to stream channel consumers regardless of subscriptions — these are not polled,
they are emitted by the daemon state machine.

**Subscription cadence:** consumer specifies `interval_ms` — the requested gap between
polls. Omitting it means the daemon cycles through all subscriptions as fast as the
bus allows, distributing bus time evenly. The daemon honours the requested interval
if the bus can support it; falls back to best-effort if subscriptions saturate
available bus time. No hard minimum or maximum enforced in Phase 2.

---

### Decision 6 — Consumer Discovery ✓ SETTLED

**Fixed default `127.0.0.1:2534` with CLI and env var override.**

Port 2534 is a direct reference to J2534. Consumers connect to the default with no
configuration required. The daemon logs its bind address at startup.

Override mechanisms (in precedence order):
- CLI flag: `--port`, `--bind`
- Environment variable: `BACKPROBE_PORT`, `BACKPROBE_BIND`
- Default: `127.0.0.1:2534`

No mDNS or service discovery in Phase 2.

---

### Decision 7 — Client Library Scope ✓ SETTLED

**Synchronous for Phase 2.** The client library wraps the TCP connection and
JSON-RPC framing and returns plain Python objects. Consumers use threads if they
want the admin and stream channels running simultaneously — which the two-channel
design already encourages naturally.

Async is a deliberate future consideration, not an oversight. J2534 devices support
multiple CAN networks simultaneously (HS-CAN, MS-CAN, LIN on one device), and a
future multi-network plugin would benefit from async to manage concurrent bus traffic
efficiently. When that work is scoped, the client library should be revisited — the
wire protocol does not need to change, only the client's concurrency model. See
ROADMAP.md § Async / concurrency.

---

## Tier 3 Command Surface (Draft)

Commands the daemon must handle. Names are working names — final names set in the
protocol design document.

**Handshake (both channels)**

| Command | Channel | Description | Returns |
|---|---|---|---|
| `open` | both | First message on any connection. Declares channel type and establishes the connection. | confirmation or error |
| `vehicle_offer` | admin | Daemon → consumer. Sent on connect if a vehicle is present, and again when a vehicle arrives. | VIN, make, model, year, protocol, mil_on, dtc_count |
| `vehicle_accept` | admin | Consumer → daemon. Accepts the offered vehicle and opens full API access. | confirmation |

**Admin channel commands (one consumer, request/response)**

| Command | Description | Returns |
|---|---|---|
| `status` | Current daemon state: no device / no vehicle / attached | state, vehicle info if attached |
| `identity` | Vehicle identity: VIN, protocol, ECU list | VIN, make/model/year hints, ECU addresses |
| `available_params` | What parameters this vehicle supports | list of named parameters with units |
| `read` | Read one parameter right now | name, value, unit, timestamp |
| `read_many` | Read a set of parameters in one round-trip | list of name/value/unit |
| `mil_status` | MIL on/off | boolean |
| `readiness` | OBD readiness monitor status | list of monitors with ready/not-ready |
| `read_dtcs` | Stored, pending, and permanent DTCs | list of DTCs per ECU |
| `clear_dtcs` | Clear stored DTCs | confirmation or error |
| `test_results` | Mode 06 on-board monitor test results | list of test results per ECU |
| `stream_subscribe` | Subscribe to a read command on the stream channel at a given cadence | confirmation |
| `stream_unsubscribe` | Cancel a stream subscription | confirmation |
| `request` | Raw mode/data escape hatch (fenced) | raw response bytes |

**Stream channel (push, unlimited consumers)**

| Event | Description |
|---|---|
| `vehicle_connected` | Autonomous — daemon found and probed a vehicle |
| `vehicle_lost` | Autonomous — vehicle unplugged or voltage dropped |
| `device_lost` | Autonomous — J2534 device unplugged |
| `stream_data` | Subscribed command result, pushed at the requested cadence |

---

## Done When

1. A demo script written against only the API docs can: connect to a vehicle,
   read its identity, read current DTCs, stream live RPM and coolant temp, and
   clear DTCs — without importing any backprobe module or knowing anything about
   J2534 or OBD2.

2. The script works against the Supergoose Plus on the bench with a real vehicle.

3. The Python client library is documented well enough that a developer who has
   never seen the source can use it from the docs alone.

4. A consumer written in any language with a TCP socket and JSON-RPC can connect
   and call `status` and `read_dtcs` successfully (proves language-agnosticism).

---

## What Phase 2 Does Not Build

- GUI (Phase 3)
- MCP server (Phase 3)
- Network-accessible server hardening (authentication, TLS) — out of scope until
  there is a concrete deployment that needs it
- Additional transports (SocketCAN, ELM327) — architecture supports them; Phase 2
  does not add them
- Async client library — synchronous is sufficient for Phase 2 consumers

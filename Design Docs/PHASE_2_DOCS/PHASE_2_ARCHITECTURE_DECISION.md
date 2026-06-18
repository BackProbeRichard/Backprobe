# Phase 2 — Architecture Decision: Library vs. Daemon Process

*v1 — 2026-06-17. Scope question: what form does the Phase 2 consumer surface take?*

---

## The Question

Phase 1 built the machinery: device ownership, J2534 transport, probe, decode, state
machine. Phase 2 exposes that machinery to consumers (GUI, MCP, scan tool, insurance
snapshot, home automation server). The decision is **how consumers reach it**.

Two models. One decision.

---

## Model A — In-Process Library

Backprobe is a Python package. The consumer imports it, instantiates an object, and
calls methods. Everything runs in the consumer's process.

```
Consumer process
├─ consumer code           scan_tool.py / gui.py / mcp_server.py
├─ import backprobe        vehicle.read_dtcs() / vehicle.stream()
│   ├─ Watcher / Query Engine
│   ├─ J2534 transport
│   └─ Vendor DLL (loaded here)
```

**Pros**
- Simple. No sockets, no serialization, no protocol to design. Just Python method calls.
- Low latency — no IPC hop between the call and the DLL.
- Easy to embed. A scan tool vendor ships their app + `backprobe/` in one installer.
- No daemon process management. Consumer starts, Backprobe starts with it. Consumer stops, device released.
- Entire stack is in one process, one log, one crash handler.

**Cons**
- Consumer must be Python (or have Python bindings). A C# GUI or a web dashboard can't call it directly.
- J2534 DLL constraint bleeds into consumer. Consumer must also be 32-bit Python on Windows.
- Only one consumer can hold the device at a time. A second import in a second process = `DeviceBusy`.
- If the consumer's process crashes, device release depends on the crash handler working. Library crash
  handler is weaker than a managed daemon with its own lifetime.
- `stream()` requires threads or async inside the consumer's process. The consumer has to care about this.

---

## Model B — Daemon Process (Server)

Backprobe runs as a standalone process. Consumers connect over IPC and send commands.
The daemon owns the device exclusively; consumers are clients.

```
Consumer A process         Consumer B process         Consumer C process
(GUI)                      (MCP server)               (home automation)
    │                          │                           │
    └──────────────── IPC protocol (commands / responses / events) ──────┘
                               │
                       Backprobe daemon process
                       ├─ Watcher / Query Engine
                       ├─ J2534 transport
                       └─ Vendor DLL
```

**Pros**
- Language-agnostic consumers. A C# GUI, a Python MCP server, a Node.js dashboard — all
  connect the same way. The 32-bit Python / Windows constraint stays inside the daemon.
- Device ownership is clean and independent of any consumer's lifetime. Consumer crashes → device stays held, cleanly managed.
- Multiple consumers simultaneously. The GUI and a logger and an MCP agent can all
  read data at once. The daemon fans out.
- `stream()` is natural: daemon pushes events to connected consumers. Consumers just read.
- The daemon can run as a Windows service. Starts at boot, survives consumer restarts.
- Enables the "server use case": run Backprobe on a Windows box, have many network
  devices connect to it.

**Cons**
- IPC protocol to design and implement. Command format, response format, event push
  format, error serialization, versioning.
- More moving parts. Consumer code needs a client library (or raw socket handling).
- Deployment is more complex. Consumer ships daemon binary + client library + their own app.
- Slightly higher latency on every call (IPC round-trip). Negligible for OBD2 timescales but real.
- Harder to debug: two processes, two logs, one protocol between them.

---

## Decision Against Your Use Cases

You described two deployment scenarios:

> "A user would build Backprobe into a package with their consumer.
> Consider a scan tool or an insurance snapshot device."

> "A user could build a server on top of Backprobe for several devices to connect to."

> "The daemon is its own process and a 3rd party calls commands to it."

| Criterion | Library | Daemon |
|---|---|---|
| Scan tool vendor ships one installer | ✅ simple | ✅ daemon bundled with app |
| Insurance snapshot device (single consumer, embedded) | ✅ ideal | works, overkill |
| Server with many clients | ❌ one process can hold device | ✅ designed for this |
| Consumer in any language (C#, JS, etc.) | ❌ Python only | ✅ |
| GUI as separate process from device owner | ❌ fights over device | ✅ natural |
| "3rd party calls commands, daemon responds" | ❌ wrong model | ✅ exact description |
| Device released if consumer crashes | weak | ✅ daemon survives |
| Run as Windows service, always-on | awkward | ✅ natural |

**The daemon model fits your stated architecture.** The single statement "the daemon is its
own process and a 3rd party calls commands to it" is the daemon model verbatim.

The library model would fit if Backprobe was a helper module tucked inside one consumer.
But your use cases include multiple simultaneous consumers, language-agnostic access, and
a server mode — none of which the library model handles cleanly.

---

## What the Daemon Model Requires for Phase 2

Choosing the daemon model means Phase 2 must define:

1. **IPC transport** — how consumer processes connect to the daemon.
   Candidates: TCP socket (local or network), Windows named pipe, Unix domain socket.

2. **Wire protocol** — what a command looks like, what a response looks like, how
   push events (for `stream()`) are distinguished from replies.
   Candidates: JSON-RPC, newline-delimited JSON, msgpack.

3. **Client library** — a thin Python wrapper consumers import so they don't write
   raw socket code. Ships alongside Backprobe. Other languages write their own client
   or use raw sockets.

4. **Command set** — the full enumeration of what consumers can ask for.
   Drives directly from the roadmap's Tier 3 verb list:
   `connect`, `read`, `read_many`, `stream`, `available_params`, `mil_status`,
   `readiness`, `read_dtcs`, `clear_dtcs`, `test_results`, `identity`, `request`.

These are the open questions Phase 2 design must answer before code is written.

---

## Recommendation

**Build the daemon model.** It matches the architecture you described, handles every
stated use case, and keeps the messy platform constraint (32-bit Python, J2534 DLL)
isolated inside the daemon where consumers don't see it.

The next design document should settle IPC transport and wire protocol — those two
decisions gate everything else in Phase 2.

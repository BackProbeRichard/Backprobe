# Phase 1 Step 6 — Daemon (the Watcher) Design Decisions

*v1 — 2026-06-14. Child of PHASE_1_SKELETON.md (build-order step 6). Settles the
shape of `daemon/watcher.py`, `daemon/interrogate.py`, and `daemon/events.py`
before code. Each decision lists options with pros and cons, a recommendation,
and a slot to record the call. Same format as PHASE_1_VIRTUAL_DESIGN.md.*

---

## What the daemon is for

The Watcher runs unattended on the test bench. It owns the J2534 device, waits
for a vehicle to be plugged in (battery voltage on pin 16), interrogates it
(probe → VIN → ECU census → supported PIDs → MIL/DTC → Mode 06 → voltage),
writes one JSON-lines record per connection event, notices when the vehicle is
unplugged, and loops forever — releasing the device absolutely on every exit.

**Done when** (per scope): plugging the Supergoose into any 2008+ vehicle
produces a correct log entry with no typing, and stopping the daemon — including
a forced kill — releases the device so completely an OEM tool opens it
immediately after.

### Already settled (not open here)

- **Synchronous, single-threaded** + a poll loop (SKELETON §Concurrency).
- **Module split**: `watcher.py` (state machine), `interrogate.py` (probe +
  harvest), `events.py` (JSONL writer) — SKELETON confirmed.
- **Output contract**: the event stream and field shapes in
  `PHASE_1_LOG_MOCKUP.jsonl` are the contract; we conform to it.
- **Probe order**: `[11-bit/500k, 29-bit/500k, 11-bit/250k, 29-bit/250k]`.
- **Teardown ORDER is sacred** (hard rule): stop periodics → clear filters →
  disconnect → close. Phase 1 has no periodics/filters in the harvest, so it
  reduces to disconnect channel → close session — but the order generalizes.
- **Re-learn the vehicle on every connect** (hard rule). The VIN→protocol
  shortcut is explicitly "Later" (ROADMAP) — not built now.
- **Transport vocabulary only** — the daemon never sees a J2534 call or an NRC.

What's left are seven decisions about *how the daemon behaves*.

---

## Decision 1 — The state machine: states and how it's represented

The Watcher is a loop over a few clear states. The session log already expects
named states (`STATE from='SCANNING' to='HOLDING'`), so the states are explicit;
the question is how to structure them.

**The states** (proposed):

| State | Meaning | Leaves to |
|-------|---------|-----------|
| `SCANNING` | no device held; enumerate + try `open()` | `HOLDING` on open success |
| `HOLDING` | device held, no vehicle; poll voltage | `INTERROGATING` on voltage rise; `SCANNING` on `DeviceLost` |
| `INTERROGATING` | vehicle present; probe + harvest | `ATTACHED` on success; `HOLDING` if probe finds no vehicle; `SCANNING` on `DeviceLost` |
| `ATTACHED` | connected + harvested; poll for drop | `HOLDING` on voltage drop (after release); `SCANNING` on `DeviceLost` |

`DeviceLost` from **any** state → `release()` → `SCANNING` (see Decision 6): the
daemon recovers, it does not exit.

### Option A — Explicit `Enum` of states + a dispatch loop
Each state is a handler (`_scanning() -> State`, …) returning the next state; the
loop calls the current handler and logs every transition.

- **Pro:** Transitions are data — one place logs every `STATE from→to`, matching
  the logging standard.
- **Pro:** Each handler is testable in isolation against the virtual backend.
- **Pro:** Error recovery is trivial: a handler just returns `SCANNING`.
- **Con:** A little dispatch boilerplate.

### Option B — Implicit nested loops
`while True: wait_for_device(); while held: wait_for_vehicle(); interrogate(); monitor()`.

- **Pro:** Least code; reads top-to-bottom.
- **Con:** Transitions are implicit — uniform STATE logging is awkward.
- **Con:** Recovery from `DeviceLost` mid-harvest means breaking out of nested
  loops; tangled and easy to get wrong.
- **Con:** Can't test "what does HOLDING do" without the whole stack.

### Option C — State pattern (a class per state)
- **Pro:** Textbook-clean OO.
- **Con:** Overkill for four states; indirection and extra surface for no gain.

**Recommendation:** **Option A.** Explicit states + handler dispatch fits the
required STATE logging, makes each state testable against the virtual backend,
and turns error recovery into "return the state to go to."

> **Decision:** Option A - sounds easier to modify our decision tree if we need to add or change states, great for if we need to modify for other transports

---

## Decision 2 — How the daemon gets its backend (the testability seam)

Step 6 must run end-to-end against the **virtual** backend on Linux, and against
**J2534** on the Windows bench, with the same watcher code.

### Option A — Constructor injection; `__main__` selects
`Watcher(backend: TransportBackend, ...)`. `__main__` builds `J2534Backend()` on
Windows, or `VirtualBackend(attached=…)` when `--virtual`/`BACKPROBE_BACKEND=virtual`.

- **Pro:** The whole point of step 5 pays off — the daemon is tested on Linux
  against the fake vehicle, deterministically.
- **Pro:** The watcher never imports a concrete backend; honest layering.
- **Con:** One constructor argument to thread through.

### Option B — Daemon auto-detects (try J2534, fall back to virtual)
- **Pro:** Zero-config.
- **Con:** Hides which backend actually ran — a DLL-load failure on the bench
  would *silently run the fake vehicle*. Dangerous.
- **Con:** Non-deterministic tests.

**Recommendation:** **Option A.** Inject the backend; `__main__` picks it
explicitly and logs which one. Default J2534 on Windows; `--virtual [preset]`
for dev. This is what makes the daemon testable before any DLL exists.

> **Decision:** option A - sounds like it's possible to inject commanded back ends for testing or if the computer has only ELM or socketCAN drivers, no j2534 drivers (for future)

---

## Decision 3 — Vehicle-presence detection (threshold, hysteresis, cadence)

The daemon decides "plugged in" / "unplugged" from `read_voltage()` (millivolts).
Real voltage is noisy and dips hard during cranking (can sag to ~6–9 V), so a
naive single threshold will flap and fire spurious disconnects.

### Option A — Hysteresis + debounce + 1 s poll
Present when voltage **> 9000 mV**; absent when **< 7000 mV** (the gap is
hysteresis); require **2 consecutive** readings before transitioning; poll once
per second in HOLDING and ATTACHED.

- **Pro:** Rides out cranking dips and electrical noise — no false disconnects.
- **Pro:** 1 s is responsive enough for a human plugging in, cheap on CPU/log.
- **Con:** Two thresholds + a debounce count to tune (and confirm on the bench).

### Option B — Single threshold, no debounce
Present iff voltage > 9000 mV, checked each second.

- **Pro:** Simplest.
- **Con:** Flaps when voltage hovers near 9 V; a crank dip reads as a disconnect
  mid-session. Exactly the failure a bench daemon must not have.

**Recommendation:** **Option A.** Constants (`PRESENT_MV=9000`, `ABSENT_MV=7000`,
`DEBOUNCE=2`, `POLL_S=1.0`) live in one place, defaulted for a 12 V system and
flagged to verify against a real crank on the bench.

> **Decision:** _Option A is best - easy to tune later if needed - can default it to be option B if desired later on by changing the values

---

## Decision 4 — What makes a connection profile "win" the probe

The probe tries profiles in order until one connects. **Key fact:** on real
J2534, `connect()` opens a CAN channel *locally* and succeeds even if no vehicle
answers — so `connect()` succeeding is **not** proof of the right protocol.
(The virtual backend's `connect()` rejects mismatches, but the daemon must not
depend on that, or it'll behave differently on real hardware.)

### Option A — Connect **and** confirm with a query
A profile wins only if `connect()` succeeds **and** a confirming functional
request (Mode 01 PID 00) returns ≥1 valid reply within a short window. Otherwise
disconnect and try the next. A profile is also "lost" if `connect()` raises
`ConnectError`. Either way → advance.

- **Pro:** Works identically on virtual (mismatch → `ConnectError`; match →
  reply) and J2534 (mismatch → connects but no reply → advance).
- **Pro:** The confirming `0100` reply *is* the first ECU-census datum — no waste.
- **Con:** One extra request per probe step (milliseconds).

### Option B — Connect alone wins
- **Pro:** Simplest.
- **Con:** On real hardware every profile "connects" → always picks step 1 →
  wrong protocol on a 29-bit vehicle. Defeats the probe loop entirely.

**Recommendation:** **Option A.** "Won" = connected + a real reply to `0100`.
The daemon treats `ConnectError` *or* a connect-with-no-reply as "didn't win"
and advances. Reuse the `0100` reply to start the census.

> **Decision:** Option A - I want the full loop to run twice before throwing a "no protocols" error or however we have that set up. The idea is we tried twice to find the right protocol. 

---

## Decision 5 — Harvest sequence and partial-failure policy

Harvest order (from the mockup): probe → `vin_read` → `ecu_census` (ask_all
`0100`) → per-ECU `supported_pids` walk → `mil_and_dtc` → `mode_06` (MID walk +
records) → `voltage_read` → `connection_complete`. A real vehicle will lack some
of these (no Mode 06, an odd VIN, a quiet second ECU). What then?

### Option A — Best-effort, record what we got
Each step is isolated; on `Timeout`/`NotSupported`, log it loudly, emit that
event empty (or skip it), and continue. `connection_complete` reflects what
succeeded (e.g. `mode_06_supported: false`). Minimum bar for "a connection":
the census found ≥1 responding ECU (else it's a failed probe, not a connection).

- **Pro:** Matches "fail loudly in logs, gracefully in behavior." A quirky
  vehicle still yields a useful record.
- **Con:** `connection_complete` can be partial — must be honestly marked.

### Option B — All-or-nothing
Any step failure aborts the whole connection; no `connection_complete`.

- **Pro:** Records are always complete.
- **Con:** A vehicle with no Mode 06 (common) produces *nothing*. Unacceptable
  for a watcher whose job is to report what's there.

**Recommendation:** **Option A.** Best-effort per step; ≥1 ECU answering `0100`
is the bar for emitting a connection. Everything else degrades and is recorded
as present/absent. A vehicle that vanishes mid-harvest (voltage drop /
`DeviceLost`) aborts with **no** `connection_complete` — a "complete" record
means the vehicle stayed present throughout.

> **Decision:** Option A - I understand why you presented option B, but no results is worse than partial results - we can debug or work with partial results. Consider a good DTC pull with no live data or mode 06 is better than nothing at all - we at least know why the CEL is on

---

## Decision 6 — Releasing the device: clean stop AND the device vanishing under us

Hard rule: the device is released — completely, in sacred order (stop periodics →
clear filters → disconnect → close; Phase 1 reduces to disconnect → close) — no
matter how the daemon ends. But "ends" hides two situations the bench will throw
at us, and they need *opposite* outcomes:

- **(i) The program is told to stop.** The human is done and quits it → **exit**.
- **(ii) The hardware vanishes while the daemon runs.** They "got what they
  needed" and unplug the OBD cable, pull the device's USB, or cut power, without
  touching the program — the held handle may now be dead → **recover**, don't die.

Both share one mechanism; only the outcome differs.

### The model — many triggers, one shutdown path

The daemon core exposes one idempotent `request_stop()`; every stop affordance is
just a caller of it. `request_stop()` runs one idempotent `release()`
(disconnect → close, sacred order) and exits. No affordance owns its own teardown.

| Stop trigger | How | Notes |
|---|---|---|
| Close the terminal window | `CTRL_CLOSE_EVENT` (Win, `SetConsoleCtrlHandler` via ctypes) / `SIGHUP` (Unix) | Natural "I'm done" for an unattended tool; Windows gives ~5 s before force-kill, so `release()` must be fast (it is) |
| `q` keypress | non-blocking stdin check each poll tick (`msvcrt.kbhit` / `select`) | Console convenience when someone's watching |
| OS stop | `SIGTERM` / `SIGBREAK` (Task Manager, `Stop-Process`) | The "kill it" path; still releases |
| *(later)* tray Exit / GUI button | calls `request_stop()` | Deferred (a tray needs a GUI dep, against Phase 1's zero-dep posture); the seam is ready |

**Ctrl-C is deliberately NOT a stop.** `SIGINT` is ignored, with a one-time hint
(*"Ctrl-C does nothing here — press `q` or close the window"*). It's a copy
gesture in Windows terminals and an accidental-quit hazard for an unattended
watcher; stopping by interrupt is a foreground-CLI convention, not a daemon one.
Nothing relies on it — the device is still freed by the release net below, no
matter how the process actually ends.

### Recovery, not exit, when the device vanishes (situation ii)

`DeviceLost` from **any** state → `release()` (drops our stale handle so a re-plug
can re-open) → **SCANNING** → wait for the device to re-enumerate → re-open. Only
`request_stop()` exits the daemon; an unplug never does. `release()` must
therefore be **tolerant of an already-dead handle**: never block, never raise —
on a physical unplug there may be nothing left to release (the OS reclaimed it) or
the DLL handle is invalid. Virtual's `close()`/`disconnect()` already meet this;
the J2534 backend must match the contract.

### The release mechanism (how we guarantee it runs)

### Option A — Layered, all calling one idempotent `release()`
`try/finally` around the run loop (normal exit + uncaught crash) + the
console-close handler and `SIGTERM`/`SIGBREAK` handlers (which call
`request_stop()`) + an `atexit` backstop. `SIGINT` ignored.

- **Pro:** Covers window-close, OS stop, `q`, normal exit, and crashes; `atexit`
  catches anything that slips through.
- **Con:** On a *forced* kill (Task Manager "End Task", power loss) the OS may not
  let `release()` finish — but the kill/unplug frees the device anyway. The
  console-close handler covers the graceful-close case; `atexit` does not run on a
  forced close, so it is the backstop, not the primary hook.

### Option B — Context managers only (`with session, channel`)
- **Pro:** Clean for normal + exception paths.
- **Con:** A bare signal or console-close bypasses `with`. Insufficient alone.

### Option C — `atexit` only
- **Con:** Doesn't run on a forced console-close or hard kill. Insufficient.

**Recommendation:** **Option A**, reframed around the model above: one idempotent,
dead-handle-tolerant `release()`; many triggers funnel through `request_stop()`;
**Ctrl-C neutralized**; the device is released however the process ends; and a
vanished device **recovers to SCANNING** rather than exiting. The session_log
crash handler logs crashes — releasing the device is the daemon's job.

> **Decision:** Option A — Ctrl-C neutralized (SIGINT ignored); window-close, `q`, and SIGTERM/SIGBREAK (plus a future tray Exit) are the stops, all funneling through one `request_stop()` → idempotent, dead-handle-tolerant `release()`. The device is freed however we end; an unplugged device recovers to SCANNING instead of exiting.

---

## Decision 7 — The event-log writer (`events.py`)

The JSONL connection-event stream — distinct from the diagnostic `session_log`
text file. Defines durability and how closely it tracks the mockup.

### Option A — One file per run, flush per event, console mirror
A timestamped `.jsonl` per daemon run; append one line per event; `flush()`
(and `fsync` optional) after each so a kill loses nothing. A generic
`emit(event_type, **fields)` with thin typed wrappers matching the mockup's
names. Mirror key events (state changes, `connection_complete`) to stdout for
live watching.

- **Pro:** A forced kill on the bench still leaves a complete-to-the-second log
  — the whole point of a watcher you might kill.
- **Pro:** Conforms to the contract; readable live.
- **Con:** Per-event flush I/O — negligible at human/vehicle cadence.

### Option B — Buffered, flush on exit
- **Pro:** Less I/O.
- **Con:** A kill loses the most recent events — exactly when you want them most.

**Recommendation:** **Option A.** Flush-per-event durability + mockup-faithful
schema + a console mirror of the headline events.

> **Decision:** Option A

---

## Summary

| # | Decision | Recommendation |
|---|----------|----------------|
| 1 | State machine | A — explicit Enum states + handler dispatch |
| 2 | Backend source | A — constructor injection; `__main__` selects |
| 3 | Presence detection | A — hysteresis + 2-read debounce, 1 s poll |
| 4 | Probe "win" | A — connect **and** confirming `0100` reply |
| 5 | Harvest policy | A — best-effort partial; ≥1 ECU = a connection |
| 6 | Release / stop | A — one `request_stop()` → idempotent `release()`; Ctrl-C off; unplug recovers, not exits |
| 7 | Event writer | A — one file/run, flush per event, console mirror |

Once recorded, the daemon gets built to match — `events.py`, then
`interrogate.py`, then `watcher.py` — and tested entirely against the virtual
backend: a full SCANNING→HOLDING→INTERROGATING→ATTACHED→(drop)→HOLDING cycle,
a partial-harvest vehicle (no Mode 06), the probe advancing past a wrong profile
(the 29-bit preset), device contention/loss recovery, and a Ctrl-C that releases
the device.

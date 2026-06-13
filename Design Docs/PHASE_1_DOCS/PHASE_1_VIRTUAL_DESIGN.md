# Phase 1 Step 5 — Virtual Backend Design Decisions

*v1 — 2026-06-13. Child of PHASE_1_SKELETON.md (build-order step 5). Settles
the shape of `transport/virtual.py` before code. Each decision lists the
options with pros and cons, a recommendation, and a slot to record the call.*

---

## What the virtual backend is for

`virtual.py` is a fake `TransportBackend` — pure Python, no hardware — that
implements the same `base.py` interface as the real J2534 backend. Two jobs:

1. **First runnable milestone.** It exercises the whole transport interface on
   the Linux dev box, before any DLL exists.
2. **The daemon's test rig.** Step 6 (the daemon) is built and tested entirely
   against this. If the fake vehicle is convincing, a daemon that works here
   works on real hardware — and a bench failure then points squarely at the
   ctypes/DLL layer, nothing above it.

The guiding principle: **the more honestly it fakes a vehicle, the more the
daemon is actually tested.** A backend that says "yes" to everything tests
nothing.

---

## Decision 1 — Where does the vehicle's data come from?

The backend has to answer probes with real, decodable OBD bytes: VIN, supported
PIDs, MIL/DTC, Mode 06. Where do those bytes come from?

### Option A — Vehicle as a data profile, bytes synthesized from it
A `VirtualVehicle` object holds the *meaning* (VIN string, list of ECUs, each
ECU's supported PID numbers, DTC count, Mode 06 tests). The backend encodes
that into wire bytes on demand.

- **Pro:** A vehicle is described in plain terms — easy to read, easy to add
  new ones. "Give me a 2-ECU diesel with the MIL on" is a few lines of data.
- **Pro:** The encoders become the mirror of `decode.py`, enabling a
  round-trip test: `decode(encode(x)) == x`. That tests *both* sides at once.
- **Pro:** Naturally supports multiple ECUs and odd cases (no VIN, empty
  Mode 06) as data, not special-case code.
- **Con:** More code up front — we write byte *encoders*, not just literals.
- **Con:** An encoder bug could mask a decoder bug if both share a wrong
  assumption (mitigated by anchoring a few tests to the hand-written mockup
  bytes, not the encoder).

### Option B — Replay the log mockup
Reconstruct responses directly from `PHASE_1_LOG_MOCKUP.jsonl`'s two vehicles.

- **Pro:** Ties the backend exactly to the agreed output contract.
- **Pro:** Least invented data — the mockup already defines two vehicles.
- **Con:** The mockup is *decoded* values, not wire bytes — we'd still have to
  encode them, so it's Option A with a more awkward data source.
- **Con:** Rigid. Testing a new scenario means editing a JSONL fixture by hand.

### Option C — Hardcode one vehicle's raw bytes
A dict of `request → response` byte literals for a single vehicle.

- **Pro:** Dead simple, fastest to write, no encoder logic.
- **Pro:** The bytes are unambiguous — what you write is what's sent.
- **Con:** One vehicle only; multi-ECU census (acceptance test #3) is painful.
- **Con:** Adding/altering a scenario means hand-assembling hex. Error-prone.
- **Con:** No round-trip test value.

**Recommendation:** **Option A.** The round-trip test and multi-ECU support are
worth the encoder code, and seeding it with the mockup's two vehicles as presets
gives us Option B's contract-fidelity for free.

> **Decision:** ___________________________________________

---

## Decision 2 — How is "a vehicle gets plugged in" simulated?

The daemon polls `read_voltage()` and waits for it to cross a threshold
(~6 V) to move HOLDING → INTERROGATING. Voltage has to change somehow.

### Option A — Programmatic: `plug_in(vehicle)` / `unplug()`
Test or script code flips presence explicitly; `read_voltage()` returns ~12,600
mV when attached, ~200 mV when not.

- **Pro:** Deterministic — tests control exactly when the transition happens.
- **Pro:** Models the real event (a cable being connected) directly.
- **Pro:** Trivially supports the unplug → HOLDING path too (acceptance: voltage
  drop → disconnect).
- **Con:** A fully hands-free demo needs a little driver script to call it.

### Option B — Timed/scripted timeline
Voltage follows a clock: low for N seconds, then high, maybe drop later.

- **Pro:** Hands-free — run the daemon, watch it react on its own.
- **Con:** Non-deterministic in tests (timing races, slow CI).
- **Con:** Hard to express "now unplug" at an exact moment in a test.

### Option C — Start already attached
Voltage is high from the first read; vehicle present immediately.

- **Pro:** Simplest possible first run — daemon goes straight to interrogating.
- **Con:** Never tests the SCANNING/HOLDING wait or the unplug path.

**Recommendation:** **Option A** as the primitive, with C as a convenience flag
(`VirtualSession(attached=vehicle)`) for the simplest demo. B can be a thin
script over A later if we want a hands-free showcase.

> **Decision:** ___________________________________________

---

## Decision 3 — Does the probe loop actually have to work?

The daemon tries connection profiles in order — `[11-bit/500k, 29-bit/500k,
11-bit/250k, 29-bit/250k]` — until one connects. Should the fake vehicle make
it earn that?

### Option A — Vehicle declares its profile; `connect()` rejects mismatches
The vehicle knows it's, say, 11-bit/500k. `connect()` succeeds only on a match
and raises `ConnectError` otherwise.

- **Pro:** The daemon's probe loop is genuinely exercised — wrong profiles fail,
  the loop advances, the right one wins (and we can log the winning step).
- **Pro:** We can set a preset to 29-bit to prove the loop tries more than one.
- **Con:** Slightly more logic in `connect()` and the vehicle profile.

### Option B — `connect()` accepts anything
First profile always wins.

- **Pro:** Less code.
- **Con:** The probe loop after step 1 is never tested — a real defect there
  would sail through every virtual test and only surface on the bench, which is
  exactly what this backend exists to prevent.

**Recommendation:** **Option A.** Testing the probe is a core reason the backend
exists; accepting anything defeats it.

> **Decision:** ___________________________________________

---

## Decision 4 — Where do the byte encoders live?

Synthesizing wire bytes (VIN→Mode 09, PID set→bitmask, Mode 06→records) is the
inverse of `decode.py`. Where does that code belong?

### Option A — Local to `virtual.py`
Encoders are private helpers in the virtual backend.

- **Pro:** Keeps the product core (`obd2/`) lean — encoding is test-harness
  code, and nothing in the shipped product needs to *produce* OBD requests'
  answers.
- **Pro:** Clear ownership: the only thing that fakes a vehicle owns the faking.
- **Con:** If a second consumer ever needs encoding, we'd extract it then.

### Option B — A shared `obd2/encode.py`
Encoders sit beside `decode.py` as a public module.

- **Pro:** Symmetry — `encode`/`decode` side by side reads nicely.
- **Pro:** Round-trip tests import both from `obd2/` cleanly.
- **Con:** Adds product surface for a need that doesn't exist yet (only the
  fake backend encodes). Speculative.

**Recommendation:** **Option A.** Keep it local; extract to `obd2/encode.py` the
day a real consumer needs it, not before. (Round-trip tests can still import the
helpers from `virtual.py`.)

> **Decision:** ___________________________________________

---

## Decision 5 — Timing / latency

Real hardware takes milliseconds per exchange; the logs record `elapsed_ms`.

### Option A — Near-instant, optional latency knob (default 0)
Exchanges return immediately; an optional `latency_ms` per session adds a small
sleep if we want realistic logs.

- **Pro:** Fast, deterministic tests by default.
- **Pro:** Can dial in realistic timing for a demo without touching test speed.
- **Con:** Default-0 logs show `elapsed_ms≈0`, slightly unrealistic.

### Option B — Always simulate a small fixed delay
Every exchange sleeps a few ms.

- **Pro:** Logs look like the real thing out of the box.
- **Con:** Slows the test suite for no test benefit; timing in tests is a smell.

**Recommendation:** **Option A** — instant by default, latency available.

> **Decision:** ___________________________________________

---

## Summary

| # | Decision | Recommendation |
|---|----------|----------------|
| 1 | Vehicle data source | A — data profile, bytes synthesized; mockup presets |
| 2 | Plug-in simulation | A — programmatic plug_in/unplug (+ C convenience flag) |
| 3 | Probe loop honesty | A — declare profile, reject mismatches |
| 4 | Encoder location | A — local to virtual.py |
| 5 | Timing | A — instant default, optional latency |

Once these are recorded, `virtual.py` gets built to match, with tests covering:
the full SCANNING→…→ATTACHED path against a preset vehicle, the multi-ECU
census, the probe loop advancing past a wrong profile, and a decode↔encode
round-trip.

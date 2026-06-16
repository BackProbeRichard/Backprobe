# Phase 1 Step 7 — J2534 Backend Design Decisions

*v1 — 2026-06-14. Child of PHASE_1_SKELETON.md (build-order step 7). Settles the
shape of `transport/j2534.py` — the real ctypes/DLL backend — before code. Same
options→recommendation→decision-slot format as the virtual and daemon docs.*

---

## What the J2534 backend is for

The real `TransportBackend`: it owns a physical J2534 pass-thru device (the Opus
IVS Supergoose Plus is our validated target) through its 32-bit stdcall DLL, and
implements the exact `base.py` interface the daemon already drives against the
virtual backend. When this works on a real vehicle, Phase 1 is done.

Everything above this layer — decode, interrogate, the watcher — is already
proven against the virtual backend. So step 7 isolates the one thing that
*cannot* be proven on the dev box: real ctypes calls into a real DLL talking to
a real car.

## The defining constraint — no Linux validation

`j2534.py` can be **written** on Linux but **not validated** there: no DLL, no
Windows, no vehicle. So the work splits in two, and the split drives Decision 1:

- **Buildable + unit-testable on Linux** — the protocol logic: the connect
  sequence, the read-loop's echo/first-frame skipping, `ask`/`ask_all`,
  teardown order, error mapping, CAN-ID framing. Provable with a **fake DLL**.
- **Bench-only** — does the real DLL load under 32-bit Python, does a real ECU
  answer, do multi-frame replies complete, does the device truly release. The
  Bench Validation Checklist at the end is the real "done."

The fake-DLL seam is the J2534 analog of the virtual backend: it lets us land
step 7 with real test coverage of our logic, so a bench failure points at the
DLL/timing/vehicle, not at us.

## Already settled (from OldVer, PHASE_1_SCOPE, constants.py)

OldVer's `j2534.py` is a working prior subset — read for decisions, not copied.
These are fixed and not re-litigated here:

- **ISO15765 channel** (protocol `0x06`): the DLL does all ISO-TP segmentation
  and reassembly. No isotp logic on our side.
- **32-bit Python, `ctypes.WinDLL` (stdcall)**; the DLL is loaded **lazily** so
  importing the package on Linux never touches `winreg`/`WinDLL`.
- **Connect sequence:** `PassThruOpen` → `PassThruConnect(ISO15765, bitrate,
  flags)` → `Ioctl(SET_CONFIG, LOOPBACK=0)` → `PassThruStartMsgFilter(...)`.
- **Wire format:** request/response data is `[CAN_ID, 4 bytes big-endian] +
  payload`; TX uses `TxFlags = ISO15765_FRAME_PAD`; 29-bit adds `CAN_29BIT_ID`.
- **Read-loop skips:** TX echo frames (`RxStatus & TX_MSG_TYPE`) and the ISO-TP
  first-frame marker (`RxStatus & START_OF_MESSAGE`, empty payload); then strip
  the 4-byte CAN-ID prefix. The first 4 bytes of a received message ARE the
  responder's CAN ID → `Reply.ecu`.
- **Flow-control filters point at the PHYSICAL request ID** (OldVer's hard-won
  lesson: FC on the functional `0x7DF` hangs every multi-frame reply forever).
- **Registry enumeration:** scan HKLM+HKCU, `PassThruSupport.04.04` and its
  `WOW6432Node`, read `FunctionLibrary`, dedup by DLL path, skip dead entries.
- **Numbers** (protocol/filter/ioctl/flag/error IDs, addressing ranges) already
  live in `transport/constants.py`.
- **Synchronous** — no `asyncio`, no I/O lock (OldVer needed one only for its
  concurrent pollers; we are single-threaded).

---

## Decision 1 — The fake-DLL seam (what makes step 7 testable on Linux)

To unit-test the protocol logic without hardware, the DLL must be replaceable.

### Option A — A thin DLL wrapper the backend talks to; real or fake injected
`j2534.py` calls the device through a small `_J2534Lib` object exposing the ~10
PassThru functions we use. On Windows it wraps `ctypes.WinDLL`; tests inject a
`FakeJ2534Lib` that returns canned `PASSTHRU_MSG` bytes for a scripted vehicle.

- **Pro:** The connect sequence, read loop, `ask`/`ask_all`, teardown, and error
  mapping all run under pytest on Linux — the same payoff the virtual backend
  gave the daemon.
- **Pro:** The fake can be backed by the existing `VirtualVehicle` encoders, so
  J2534 tests exercise the same real-capture-seeded vehicles.
- **Con:** A wrapper layer to define (small — it's a 1:1 pass-through).

### Option B — Direct `WinDLL` calls inline
- **Pro:** Least code.
- **Con:** Zero test coverage off the bench. Every logic bug waits for Windows +
  a vehicle to surface — exactly what this project is structured to avoid.

**Recommendation:** **Option A.** A minimal injectable `_J2534Lib`; a
`FakeJ2534Lib` (ideally driven by the virtual presets' encoders) makes the logic
provable on Linux. Without this, step 7 is untested code until the bench.

> **Decision:** Option A

---

## Decision 2 — Filter & addressing for the census and 29-bit

OldVer installed ONE flow-control filter for ONE 11-bit ECU. We need the daemon's
multi-ECU census (`ask_all` on the functional broadcast) AND multi-frame replies
from any ECU AND both addressing modes. A J2534 filter is also what lets us
*receive* at all, so the filter set defines who we can hear.

### Option A — Install the standard FC filter set for all 8 OBD ECUs at connect
At connect, for the profile's addressing, install one FLOW_CONTROL filter per OBD
ECU: 11-bit pattern `0x7E8+i` ↔ flowctrl `0x7E0+i`; 29-bit pattern `0x18DAF1<ii>`
↔ flowctrl `0x18DA<ii>F1`, for i in 0..7. Requests are written to the functional
ID (`0x7DF` / `0x18DB33F1`).

- **Pro:** Any responding ECU is both heard (census) and flow-controlled
  (multi-frame) with no per-vehicle setup. 8 filters ≤ the spec's max of 10.
- **Pro:** Addressing-aware from `constants.py` ranges; one code path, two modes.
- **Con:** Installs filters for ECUs that may not exist (harmless — they just
  never match).

### Option B — Discover ECUs first, then add FC filters lazily
- **Con:** The discovery census itself can include multi-frame replies that fail
  without an FC filter already in place — chicken-and-egg. More state, ordering
  hazards.

### Option C — Single FC filter (OldVer)
- **Con:** Hears/serves one ECU only; breaks the census and any non-engine ECU.

**Recommendation:** **Option A.** Install the full 8-ECU FC filter set for the
connected addressing mode at connect. Robust, stateless, spec-legal.

> **Decision:** Option A 

---

## Decision 3 — `ask()` vs `ask_all()` read-loop semantics

Both write the functional request; they differ in how they read. (Phase 1 needs
no physical addressing — the per-ECU PID/MID maps are built by `ask_all` grouping
replies by responder, as the daemon already does.)

### Option A — Shared exchange: clear RX, write, then read per verb
Before each exchange, `Ioctl(CLEAR_RX_BUFFER)` (drop stale/crossed frames), write
the functional request, then read in a loop skipping echo/first-frame frames:
- `ask`: return the **first** real reply (optionally matching the expected
  response SID + echoed PID), or `Timeout` when the deadline passes.
- `ask_all`: keep reading until the **window** expires, collecting **one reply
  per distinct ECU**; `[]` is valid.

- **Pro:** Deterministic — clearing RX first removes OldVer's stale-frame
  problem at the source instead of pattern-matching it away.
- **Pro:** One read primitive; the two verbs differ only in stop condition.
- **Con:** A clear-per-exchange is one extra ioctl (negligible).

### Option B — No RX clear; discard stale frames by matching SID/PID (OldVer)
- **Pro:** One fewer ioctl.
- **Con:** Relies on SID/PID matching to reject leftovers; fragile for `ask_all`
  where many SIDs are legitimately in flight.

**Recommendation:** **Option A.** Clear RX → write → read; `ask` stops at first
match, `ask_all` drains the window by ECU. SID/PID matching kept as a secondary
guard on `ask`.

**Fallback logging.** Both fallback signals are recorded (built in step 7):
- `RX_CLEAR_UNSUPPORTED` — the clear ioctl failed/unsupported, so we've fallen
  back to matching for this device. A once-per-connection *device capability*,
  so it goes in **both logs**: the session log (with the J2534 return code) and
  the event log (a structured `rx_clear_unsupported` event), where it surfaces
  in the connection record.
- `STALE_FRAME_DISCARDED` — the SID/PID match rejected a leftover frame. The
  backstop doing work; **session log only** (high-frequency debug detail).

Together they answer the bench question: does the clear work on this device, and
how hard is the match having to compensate?

> **Decision:** A + B as a safety net

---

## Decision 4 — Mapping J2534 return codes to the error taxonomy

Raw J2534 codes must never escape this layer (base.py rule). One mapper turns a
return code + call context into the right `TransportError`.

### Option A — Central map + call-site context
A `_check(ret, *, context)` helper: `STATUS_NOERROR` → ok; `ERR_DEVICE_IN_USE` →
`DeviceBusy`; `ERR_DEVICE_NOT_CONNECTED` → `DeviceLost`; `ERR_NOT_SUPPORTED` →
`NotSupported`; `ERR_BUFFER_EMPTY` → *not an error* (read loop keeps polling);
everything else → `InternalError` carrying `constants.error_name(ret)`. Connect
failures and a failed confirming query map to `ConnectError` at that call site.

- **Pro:** One place to read; the log always gets the J2534 name; the daemon
  sees only the taxonomy it already handles.
- **Con:** A small table to keep aligned with `constants.ERROR_NAMES`.

### Option B — Everything → `InternalError`
- **Con:** The daemon can't distinguish "busy, wait" from "lost, rescan" from
  "unsupported" — collapses Decision-6 recovery and the contention wait.

**Recommendation:** **Option A.** Central map; `ERR_BUFFER_EMPTY` is the normal
"no message yet" in the read loop, not a failure.

> **Decision:** option A - make a lookup table that we can edit in case we want to change what errors are assigned to taxonomy

---

## Decision 5 — ctypes `argtypes` / `restype` declarations

### Option A — Declare them explicitly when binding the DLL
Set `restype`/`argtypes` for each PassThru function in `_J2534Lib` at load.

- **Pro:** On 32-bit stdcall a mis-sized argument corrupts the stack; explicit
  types catch it and document the ABI. ctypes does pointer conversion correctly.
- **Con:** A few lines of binding per function.

### Option B — Rely on ctypes defaults (OldVer)
- **Con:** Default `int` args/return; silent truncation or stack issues possible,
  and the fake DLL wouldn't enforce a shape.

**Recommendation:** **Option A.** Declare `argtypes`/`restype` once at bind time.

> **Decision:** option A

---

## Decision 6 — Flow-control config (BS / STMIN) and the 04.04 vs 02.02 IDs

OldVer set only `LOOPBACK=0` and left ISO-TP block-size / separation-time at the
DLL's defaults. `constants.py` flags a real discrepancy: 04.04 uses
`ISO15765_BS=0x1E`, `STMIN=0x1F`, while OldVer used the 02.02 values `0x16`/`0x17`
(which mean something else in 04.04).

### Option A — Minimal config now; defaults for BS/STMIN; resolve on the bench
Set `LOOPBACK=0` at connect; leave BS/STMIN at DLL defaults (they worked for
OldVer's OBD multi-frame). Keep the 04.04 constants defined but unused until the
bench shows a multi-frame timing problem that needs them.

- **Pro:** Fewer knobs to get wrong before we can test; default ISO-TP timing is
  fine for OBD physical multi-frame in practice.
- **Con:** If a specific DLL needs explicit BS/STMIN, that surfaces on the bench.

### Option B — Set BS/STMIN explicitly now (04.04 values)
- **Con:** Tuning timing we can't observe yet; risks fighting a DLL default that
  was already correct.

**Recommendation:** **Option A.** `LOOPBACK=0` only; BS/STMIN default; the 04.04
IDs are verified (or pressed into service) on the bench.

> **Decision:** Option A - we already did the hard work

---

## Bench validation checklist (Phase 1 "done")

Logic is proven on Linux via the fake DLL; these can only pass on the Windows
bench with the Supergoose and a real 2008+ vehicle:

- [ ] 32-bit Python loads the Supergoose DLL; `enumerate()` finds it in the registry.
- [ ] `PassThruOpen`/`Connect(ISO15765, 500k, 11-bit)` succeeds on a real vehicle.
- [ ] Probe confirms with a real `0100` reply; 29-bit / 250k vehicles advance correctly.
- [ ] VIN (`0902`) reads — proves the FC filter and multi-frame reassembly.
- [ ] Census finds every ECU; Mode 06 (`0601`) completes per ECU.
- [ ] **Release is absolute:** after every stop path (`q`, window-close, SIGTERM,
      crash) and after a device unplug, an OEM tool opens the device immediately.
- [ ] Device-unplug mid-run recovers to SCANNING and re-opens on replug (daemon Decision 6).
- [ ] Resolve the BS/STMIN `0x1E/0x1F` vs `0x16/0x17` discrepancy if multi-frame misbehaves.
- [ ] Confirm voltage thresholds (present/absent/debounce) against a real crank.

---

## Summary

| # | Decision | Recommendation |
|---|----------|----------------|
| 1 | Fake-DLL seam | A — injectable `_J2534Lib`; `FakeJ2534Lib` for Linux tests |
| 2 | Filters / addressing | A — full 8-ECU FC filter set at connect, per addressing mode |
| 3 | `ask`/`ask_all` read loop | A — clear RX → write → read; first-match vs drain-window |
| 4 | Error mapping | A — central code→taxonomy map; BUFFER_EMPTY = keep polling |
| 5 | ctypes types | A — explicit `argtypes`/`restype` at bind |
| 6 | FC config | A — `LOOPBACK=0` only; BS/STMIN default; resolve on bench |

Once recorded, build order within step 7: the ctypes structures + `_J2534Lib`
binding, then `FakeJ2534Lib`, then the backend (enumerate/open/connect/exchange/
teardown) test-driven against the fake on Linux — then to the Windows bench for
the checklist above.

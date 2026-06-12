# Planned & Future Work

_Last updated: 2026-06-06 (v0.1.4)_

## In progress

## Planned for next release

## Backlog / future
- [ ] More intelligent freeze frame — tie the captured frame to the DTC that stored it, show that context, and stop exposing a manual "frame #" field for ECUs that only store frame 0 (probe availability instead of timing out 11×5s on frame 1). Observed on the 2021 Tacoma: frame 0 returns data, frame 1 times out for every PID.
- [ ] More relevant pop ups, modeled on professional scan tools (e.g. turn ignition off/on/10 seconds when clearing codes)
- [ ] Restyle the Yes/No confirmation modal (`self._confirm`) — current colors aren't great; refine palette/layout to match the app's look
- [ ] Live Data PID selection — let the user select/deselect which PIDs are polled and search/filter the PID list. Polling only a chosen subset (or a single PID) raises the effective per-PID sample rate enough to capture fast transients (e.g. a throttle snap, which at the current ~1 Hz all-PIDs cadence only caught the peak, not the rise).
- [ ] Automatic CAN FD / protocol detection — probe the vehicle on connect and auto-select classic CAN vs CAN FD (and bitrate) instead of relying on the manual CAN FD checkbox. Would subsume the `can_fd` toggle review below and remove the "values fade out when CAN FD is wrongly enabled on a classic-CAN vehicle" failure mode.
- [ ] Review `can_fd` runtime toggle — connected as True on a classic-CAN vehicle in one session
- [ ] Multi-ECU scan support — currently engine-ECU only: requests go out functionally on `0x7DF` but the J2534 receive/flow-control filter exact-matches the ECM response ID `0x7E8`, so other modules that answer the broadcast (TCM `0x7E9`, ABS, body, etc.) are dropped. Add per-module discovery + per-module receive/flow-control filters so DTCs/PIDs/monitors from non-engine modules appear.
- [ ] Bluetooth support for J2534 devices — connect to J2534 interfaces that expose a wireless/Bluetooth link instead of USB (device discovery, pairing, and selection in the Connection tab).
- [ ] Unified single-app mode — MCP server runs as an in-process toggle inside the GUI; agent activity streams to a dedicated text panel in the interface

## Completed (recent)
- [x] v0.1.1 — Release tooling: `/release` skill, `old_versions/` ZIP archive scheme, logs relocated to `obd2_mcp/logs/`, CHANGELOG and PLANNED moved into package
- [x] v0.1.2 — Project hygiene: CLAUDE.md, DEPS.md, stale file cleanup, changelog Known Issues workflow
- [x] v0.1.4 — Real-vehicle bring-up on 2021 Tacoma: fixed live-data concurrency race (per-channel I/O lock + response validation), multi-frame ISO-TP receive (flow control on `0x7E0` + START_OF_MESSAGE skip — VIN/monitors now work), Mode 06 monitor parsing (9-byte records), fail-fast batch-read timeouts, `clear_dtcs` ELM327 crash. Added auto-VIN on connect, color-coded status bar, Yes/No confirm modal, Live Data ✓ apply button.

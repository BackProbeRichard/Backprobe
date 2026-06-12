# Changelog

All notable changes to the OBD2 AI Tool are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.4] — 2026-06-06

### Added
- Live Data refresh interval now has a ✓ apply button next to the entry; the value also commits on Enter.

### Changed
- Status bar is now color-coded by connection state: **orange** when disconnected / connection failed, **white** when the adapter is connected but no vehicle has responded yet, **green** once a vehicle is detected (Mode 01 `0100` answered) and through VIN display.
- Destructive-action confirmations are now a Yes/No modal (`self._confirm`) instead of typing `CONFIRM`. The "Yes" button is danger red, "No" is the default; dialog is centered over the main window. Applied to Clear DTCs.
- Live Data refresh interval is no longer applied live as you type — it commits only on Enter or the ✓ button (invalid input is rejected and the entry restored). While Auto is on, the ✓ button is disabled and its whole box turns gray (rather than just dimming the glyph) for a clearer visual state.
- On-demand batch reads (freeze frame, Mode 06 monitors, Mode 09 vehicle info, Mode 22 DIDs) now use a dedicated `obd_read_timeout` (default 2.0s) instead of the 5.0s `send_raw` default. Combined with the per-channel I/O lock these run sequentially, so a no-data or unsupported read no longer hangs 5s × every PID — e.g. a freeze-frame read with no stored frame, or an ECU that doesn't answer Mode 22 over functional addressing.

### Fixed
- Mode 06 monitors decoded as garbage (wrong descriptions, hex/huge values, bogus PASS/FAIL) on the 2021 Tacoma once multi-frame reception started working. The parser assumed the MID appeared once with 8-byte TID records; the real SAE J1979 format repeats the MID in every record and each record is 9 bytes (`MID TID UASID Val Min Max`). After the first record the parser drifted one byte per record. Now parses 9-byte records using each record's own MID. Virtual backend updated to the correct wire format to match.
- Mode 22 UDS DID reads ignored the new `obd_read_timeout` and still waited the full 5.0s each. The udsoncan `request_timeout` doesn't cover our combined ISO-TP `send_raw` call; the connection adapter now passes the timeout through, so DID reads fail fast at 2.0s.
- `clear_dtcs()` raised `NameError` on the python-obd/ELM327 branch — it referenced an undefined `conn` instead of `obd_conn`. Clearing DTCs over an ELM327 connection would have crashed; corrected. (J2534 raw path was unaffected.)
- Live Data showed every PID as raw hex and flooded the bus with timeouts on the 2021 Tacoma. Root cause: concurrent polling (`asyncio.gather` over ~59 PIDs, each in its own executor thread) hit the single J2534 channel with no serialization, so write/read pairs interleaved and responses came back matched to the wrong request (e.g. asked `0141`, got PID `0x43`'s bytes). `_parse_pid_value` then rejected the mismatched PID and fell back to `raw_hex`. Fixed by serializing each write+read under a per-channel `threading.Lock` and validating the response against the request (positive-response SID + Mode 01/06/09 PID/MID echo), discarding stale/crossed frames.
- *(since 0.1.4)* Multi-frame ISO-TP receive over J2534 (VIN `0902`, Mode 06 monitors `0601`) never completed (open since 0.1.1) — **confirmed fixed on the 2021 Tacoma** (VIN resolved to `3TMCZ5AN2MM421733`, monitors populate). Two parts:
  1. **Flow control sent to the wrong CAN ID (root cause).** The ISO15765 flow-control filter sent the FC frame on the functional broadcast `0x7DF` instead of the ECM's physical request ID `0x7E0`. The ECM sends its First Frame and waits for FC on `0x7E0`; it never arrived, so the consecutive frames were never sent and the read timed out. Filter now uses `0x7E0` (`fc_filter_ret` confirmed `0x00000000` on the vehicle).
  2. **First-frame indication was returned as the response.** The read loop now skips the J2534 START_OF_MESSAGE indication (empty payload, `RxStatus & 0x02`) and waits for the reassembled message.

### Potentially Fixed
- Automatic VIN detection on connect — addresses the original "VIN was not recognized" complaint. Once PID discovery confirms the vehicle is awake (Mode 01 `0100` answered), a lightweight Mode 09 `0902` read auto-populates the Vehicle Info VIN field and the status bar; a "No vehicle response — check key / connection" indicator shows when `0100` goes unanswered (adapter connected, no vehicle). Verified in virtual mode; needs confirmation on the next real-vehicle connect.

### Known Issues
- *(new)* GUI Refresh rate / Ghosting on scroll
- *(since 0.1.1)* `can_fd` connected as `True` in one session despite a classic-CAN vehicle — runtime toggle that enabled it should be reviewed. Note: on the 2021 Tacoma (classic CAN), enabling CAN FD correctly causes all live values to fade out because the ECU stops answering — this is expected behavior, not a bug.
- *(new)* Mode 22 UDS DIDs (`22 F1 90` etc.) time out on the Tacoma over functional `0x7DF`. Likely needs physical addressing (`0x7E0`/`0x7E8`) rather than the OBD functional broadcast — Mode 09 is the correct VIN path regardless.
- *(new)* Mode 06 monitors with OEM/extended UAS (Unit and Scaling) IDs outside the SAE J1979 Table A.2 set (e.g. `0x8a`, `0x8d`, `0x90` seen on the Tacoma) display the raw integer with the scaling ID shown as `(scale 0xXX)` instead of a scaled value + unit. Record alignment is correct; only the engineering-unit conversion for these IDs is missing. Not expanding the table speculatively — wrong scaling factors are worse than showing raw.
- *(new)* `_exchange_sync` charges a flat 100 ms against the per-request deadline for every skipped frame (TX echo, START_OF_MESSAGE indication, or a discarded stale/crossed frame). On a busy bus several skips can exhaust a short timeout before the real answer is read, causing an occasional spurious timeout.

### Removed

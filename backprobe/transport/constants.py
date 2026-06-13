"""J2534 (SAE J2534-1 v04.04) numeric constants.

Sources, in order of authority:
  1. Drew Tech j2534_v0404.h — the header our vendor boilerplate #includes.
     Cross-checked against two independent mirrors (erik-smit/j2534-logger,
     diamondman/J2534-PassThru-Logger); all values agreed.
  2. OldVer/obd2_mcp/transport/j2534.py — prior working subset.

Tags: every constant is marked [spec] (taken from the 04.04 header) or
[hw] (confirmed against the Supergoose on the bench). Nothing is [hw] yet;
tags flip as bench testing exercises each value.

KNOWN DISCREPANCY vs OldVer: OldVer used ISO15765_BS=0x16, ISO15765_STMIN=0x17.
Those are the 02.02-spec values; in 04.04 they are 0x1E/0x1F, and 0x16/0x17
mean PARITY / BIT_SAMPLE_POINT. We follow 04.04. Verify on the bench.
"""

# ─── Registry discovery ──────────────────────────────────────────────────
# Where Windows lists installed J2534 04.04 devices. 32-bit Python on
# 64-bit Windows is redirected to the WOW6432Node view automatically.

REGISTRY_PATH = r"SOFTWARE\PassThruSupport.04.04"          # [spec]
REGISTRY_KEY_NAME = "Name"                                  # [spec]
REGISTRY_KEY_VENDOR = "Vendor"                              # [spec]
REGISTRY_KEY_LIBRARY = "FunctionLibrary"                    # [spec]

# ─── Protocol IDs (PassThruConnect) ──────────────────────────────────────

CAN = 0x05                  # [spec] raw CAN, no transport layer
ISO15765 = 0x06             # [spec] CAN with ISO-TP handled by the DLL — Phase 1 uses this

# ─── Connect flags (PassThruConnect Flags argument) ──────────────────────

CAN_29BIT_ID = 0x0100       # [spec] 29-bit addressing (also a TxFlags bit per-message)

# ─── Filter types (PassThruStartMsgFilter) ───────────────────────────────

PASS_FILTER = 0x01          # [spec]
BLOCK_FILTER = 0x02         # [spec]
FLOW_CONTROL_FILTER = 0x03  # [spec] ISO-TP flow control — required for multi-frame

# ─── Ioctl IDs (PassThruIoctl) ───────────────────────────────────────────

GET_CONFIG = 0x01           # [spec]
SET_CONFIG = 0x02           # [spec]
READ_VBATT = 0x03           # [spec] pin-16 battery voltage, millivolts — the presence signal
CLEAR_TX_BUFFER = 0x07      # [spec]
CLEAR_RX_BUFFER = 0x08      # [spec]
CLEAR_PERIODIC_MSGS = 0x09  # [spec] teardown step 1 (sacred order)
CLEAR_MSG_FILTERS = 0x0A    # [spec] teardown step 2 (sacred order)

# ─── Config parameters (GET_CONFIG / SET_CONFIG) ─────────────────────────

DATA_RATE = 0x01            # [spec] channel bitrate
LOOPBACK = 0x03             # [spec] 0 = no TX echo (we set this at connect)
ISO15765_BS = 0x1E          # [spec] block size — NOTE: OldVer used 02.02 value 0x16
ISO15765_STMIN = 0x1F       # [spec] separation time min — NOTE: OldVer used 0x17
ISO15765_WFT_MAX = 0x25     # [spec] max wait-frame transmissions

# ─── RxStatus bits (PassThruReadMsgs) ────────────────────────────────────

TX_MSG_TYPE = 0x0001        # [spec] loopback/TX-echo frame — Channel.ask skips these
START_OF_MESSAGE = 0x0002   # [spec] ISO-TP first-frame marker, empty payload — skipped

# ─── TxFlags bits (PassThruWriteMsgs) ────────────────────────────────────

ISO15765_FRAME_PAD = 0x0040  # [spec] pad single frames to 8 bytes (OBD2 requires)
# CAN_29BIT_ID (0x0100) doubles as a TxFlags bit — defined above

# ─── Return codes (every PassThru* call) ─────────────────────────────────

STATUS_NOERROR = 0x00       # [spec]

# Full 04.04 table, for logging. Raw codes never escape the transport layer;
# this maps them to names so the session log reads like English.
ERROR_NAMES: dict[int, str] = {
    0x00: "STATUS_NOERROR",
    0x01: "ERR_NOT_SUPPORTED",
    0x02: "ERR_INVALID_CHANNEL_ID",
    0x03: "ERR_INVALID_PROTOCOL_ID",
    0x04: "ERR_NULL_PARAMETER",
    0x05: "ERR_INVALID_IOCTL_VALUE",
    0x06: "ERR_INVALID_FLAGS",
    0x07: "ERR_FAILED",
    0x08: "ERR_DEVICE_NOT_CONNECTED",
    0x09: "ERR_TIMEOUT",
    0x0A: "ERR_INVALID_MSG",
    0x0B: "ERR_INVALID_TIME_INTERVAL",
    0x0C: "ERR_EXCEEDED_LIMIT",
    0x0D: "ERR_INVALID_MSG_ID",
    0x0E: "ERR_DEVICE_IN_USE",
    0x0F: "ERR_INVALID_IOCTL_ID",
    0x10: "ERR_BUFFER_EMPTY",
    0x11: "ERR_BUFFER_FULL",
    0x12: "ERR_BUFFER_OVERFLOW",
    0x13: "ERR_PIN_INVALID",
    0x14: "ERR_CHANNEL_IN_USE",
    0x15: "ERR_MSG_PROTOCOL_ID",
    0x16: "ERR_INVALID_FILTER_ID",
    0x17: "ERR_NO_FLOW_CONTROL",
    0x18: "ERR_NOT_UNIQUE",
    0x19: "ERR_INVALID_BAUDRATE",
    0x1A: "ERR_INVALID_DEVICE_ID",
}


def error_name(code: int) -> str:
    """Name for a J2534 return code, or 'UNKNOWN(0x..)' — never raises."""
    return ERROR_NAMES.get(code, f"UNKNOWN(0x{code:02X})")


# ─── ISO 15765-4 OBD addressing (SAE J1979, not J2534) ──────────────────
# Lives here because both backends need it to install connect-time filters.

OBD_FUNCTIONAL_11BIT = 0x7DF          # [spec] broadcast request ID
OBD_REQUEST_11BIT = range(0x7E0, 0x7E8)   # [spec] physical request IDs (flow-control side)
OBD_RESPONSE_11BIT = range(0x7E8, 0x7F0)  # [spec] physical response IDs (pattern side)

OBD_FUNCTIONAL_29BIT = 0x18DB33F1     # [spec] broadcast request ID
OBD_REQUEST_29BIT_BASE = 0x18DA00F1   # [spec] phys request: 0x18DA<ecu>F1
OBD_RESPONSE_29BIT_BASE = 0x18DAF100  # [spec] phys response: 0x18DAF1<ecu>

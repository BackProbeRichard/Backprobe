# Phase 2 — Wire Protocol

*v1 — 2026-06-18. Defines the exact wire format for every message exchanged between
a consumer and the Backprobe daemon. Implement from this document — do not infer
behaviour from source code.*

---

## Overview

The Backprobe daemon listens on `127.0.0.1:2534` (TCP). Every connection begins with
an `open` handshake that declares the channel type. There are two channel types:

- **Admin channel** — request/response. One consumer at a time. Used for all commands,
  subscription management, and write operations.
- **Stream channel** — daemon pushes, consumer listens. Unlimited consumers. Used for
  live data and autonomous events.

A consumer that wants both opens two connections to the same port.

---

## Framing

Every message — in both directions, on both channels — is one JSON object followed by
a newline (`\n`). The receiver reads bytes until `\n`, then parses the complete JSON
object. No length prefix. No binary envelope.

```
{"jsonrpc":"2.0","id":1,"method":"open","params":{"channel":"admin"}}\n
```

Messages never contain embedded newlines. All JSON is compact (no pretty-printing).

---

## JSON-RPC 2.0 Basics

Backprobe uses JSON-RPC 2.0. The four message shapes:

**Request** (consumer → daemon, admin channel):
```json
{"jsonrpc":"2.0","id":1,"method":"read_dtcs","params":{}}
```

**Response** (daemon → consumer, admin channel):
```json
{"jsonrpc":"2.0","id":1,"result":{"dtcs":[]}}
```

**Error** (daemon → consumer, admin channel):
```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32002,"message":"no vehicle"}}
```

**Notification** (daemon → consumer, either channel — no `id` field):
```json
{"jsonrpc":"2.0","method":"vehicle_offer","params":{...}}
```

The `id` field may be any integer or string. The daemon echoes it back on the
response. Consumers increment a counter per connection; strings are valid too.

---

## Connection Lifecycle

### 1. Open handshake

The first message on every connection must be `open`. The daemon routes the connection
to the correct handler based on the declared channel type.

**Request:**
```json
{"jsonrpc":"2.0","id":1,"method":"open","params":{"channel":"admin"}}
{"jsonrpc":"2.0","id":1,"method":"open","params":{"channel":"stream"}}
```

**Response (success):**
```json
{"jsonrpc":"2.0","id":1,"result":{"ok":true}}
```

**Error — admin channel already held:**
```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"admin channel already held"}}
```

If the open handshake fails, the daemon closes the connection immediately.

---

### 2. Vehicle offer (admin channel only)

Immediately after a successful `open` on the admin channel, the daemon sends one of:

**A vehicle is present:**
```json
{"jsonrpc":"2.0","method":"vehicle_offer","params":{
    "vin":       "1FTEW1EG7GF...",
    "make":      "Ford",
    "model":     "F-150",
    "year":      2016,
    "protocol":  "ISO15765 / CAN 11-bit 500kbps",
    "mil_on":    true,
    "dtc_count": 1
}}
```

**No vehicle currently connected:**
```json
{"jsonrpc":"2.0","method":"no_vehicle","params":{}}
```

In the `no_vehicle` case the consumer waits. When a vehicle arrives the daemon sends
`vehicle_offer` unprompted. The consumer does not need to poll.

---

### 3. Vehicle accept (admin channel only)

After receiving `vehicle_offer` the consumer sends `vehicle_accept` to open full API
access. The consumer may inspect the offer payload first — check the VIN, make, or
protocol — before deciding whether to accept.

**Request:**
```json
{"jsonrpc":"2.0","id":2,"method":"vehicle_accept","params":{}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":2,"result":{"ok":true}}
```

Until `vehicle_accept` is sent, all admin channel commands other than `status` return
error `-32002` (no vehicle session).

---

### 4. Vehicle lost

If the vehicle disconnects while a consumer session is active, the daemon sends a
notification on the admin channel and all stream channels:

```json
{"jsonrpc":"2.0","method":"vehicle_lost","params":{
    "reason":     "voltage_dropped",
    "voltage_mv": 200
}}
```

The session ends. The consumer must wait for the next `vehicle_offer`.

---

## Admin Channel — Command Reference

All commands below are request/response. The consumer sends a request; the daemon
sends exactly one response. Commands may be sent in any order after `vehicle_accept`
unless noted otherwise.

---

### `status`

Current daemon state. Available before `vehicle_accept`.

**Request:**
```json
{"jsonrpc":"2.0","id":3,"method":"status","params":{}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":3,"result":{
    "state":   "attached",
    "device":  "SuperGoose-Plus",
    "vehicle": {"vin":"1FTEW1EG7GF...","make":"Ford","model":"F-150","year":2016}
}}
```

`state` values: `"no_device"` / `"no_vehicle"` / `"probing"` / `"attached"`.
`vehicle` is omitted when state is not `"attached"`.

---

### `identity`

Full vehicle identity as interrogated during probe.

**Request:**
```json
{"jsonrpc":"2.0","id":4,"method":"identity","params":{}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":4,"result":{
    "vin":      "1FTEW1EG7GF...",
    "make":     "Ford",
    "model":    "F-150",
    "year":     2016,
    "protocol": "ISO15765 / CAN 11-bit 500kbps",
    "ecus": [
        {"address":"0x7E8","name":"Engine"},
        {"address":"0x7E9","name":"Transmission"}
    ]
}}
```

---

### `available_params`

All parameters this vehicle supports, by name, with units. The consumer uses this to
know what names are valid for `read`, `read_many`, and `stream_subscribe`.

**Request:**
```json
{"jsonrpc":"2.0","id":5,"method":"available_params","params":{}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":5,"result":{
    "params": [
        {"name":"rpm",           "description":"Engine RPM",                  "unit":"rpm"},
        {"name":"coolant_temp",  "description":"Engine Coolant Temperature",  "unit":"°C"},
        {"name":"vehicle_speed", "description":"Vehicle Speed",               "unit":"km/h"},
        {"name":"throttle_pos",  "description":"Throttle Position",           "unit":"%"}
    ]
}}
```

---

### `read`

Read one parameter right now. Single round-trip to the vehicle.

**Request:**
```json
{"jsonrpc":"2.0","id":6,"method":"read","params":{"name":"rpm"}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":6,"result":{
    "name":      "rpm",
    "value":     2350,
    "unit":      "rpm",
    "timestamp": "2026-06-18T12:00:00.123Z"
}}
```

---

### `read_many`

Read multiple parameters in one call. The daemon sequences the requests internally;
the consumer sees one response with all values.

**Request:**
```json
{"jsonrpc":"2.0","id":7,"method":"read_many","params":{
    "names": ["rpm","coolant_temp","vehicle_speed"]
}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":7,"result":{
    "timestamp": "2026-06-18T12:00:00.123Z",
    "readings": [
        {"name":"rpm",           "value":2350, "unit":"rpm"},
        {"name":"coolant_temp",  "value":92,   "unit":"°C"},
        {"name":"vehicle_speed", "value":0,    "unit":"km/h"}
    ]
}}
```

---

### `mil_status`

MIL (malfunction indicator lamp) on or off.

**Request:**
```json
{"jsonrpc":"2.0","id":8,"method":"mil_status","params":{}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":8,"result":{"mil_on":true}}
```

---

### `readiness`

OBD readiness monitor status for all monitors on this vehicle.

**Request:**
```json
{"jsonrpc":"2.0","id":9,"method":"readiness","params":{}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":9,"result":{
    "monitors": [
        {"name":"catalyst",        "ready":true},
        {"name":"oxygen_sensor",   "ready":true},
        {"name":"evap_system",     "ready":false},
        {"name":"egr_system",      "ready":true}
    ]
}}
```

---

### `read_dtcs`

Stored, pending, and permanent DTCs from all ECUs.

**Request:**
```json
{"jsonrpc":"2.0","id":10,"method":"read_dtcs","params":{}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":10,"result":{
    "dtcs": [
        {"code":"P0300","type":"stored",  "description":"Random/Multiple Cylinder Misfire Detected","ecu":"Engine"},
        {"code":"P0301","type":"pending", "description":"Cylinder 1 Misfire Detected",              "ecu":"Engine"},
        {"code":"P0300","type":"permanent","description":"Random/Multiple Cylinder Misfire Detected","ecu":"Engine"}
    ]
}}
```

`type` values: `"stored"` / `"pending"` / `"permanent"`.
Empty `dtcs` array means no codes present.

---

### `clear_dtcs`

Clear all stored DTCs. Permanent DTCs cannot be cleared by Mode 04 — they are
included in the response to indicate they survived the clear.

**Request:**
```json
{"jsonrpc":"2.0","id":11,"method":"clear_dtcs","params":{}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":11,"result":{
    "ok": true,
    "permanent_remaining": ["P0300"]
}}
```

`permanent_remaining` is empty when no permanent codes survived.

---

### `test_results`

Mode 06 on-board monitor test results from all ECUs.

**Request:**
```json
{"jsonrpc":"2.0","id":12,"method":"test_results","params":{}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":12,"result":{
    "tests": [
        {
            "ecu":         "Engine",
            "mid":         "0x01",
            "tid":         "0x87",
            "description": null,
            "actual":      67.0,
            "min":         0.0,
            "max":         650.0,
            "unit":        "ms",
            "passed":      true
        }
    ]
}}
```

---

### `stream_subscribe`

Subscribe to any read command on the stream channel at a given cadence. Returns a
`subscription_id` used to cancel the subscription later.

**Request:**
```json
{"jsonrpc":"2.0","id":13,"method":"stream_subscribe","params":{
    "command":     "read",
    "params":      {"name":"rpm"},
    "interval_ms": 200
}}
```

`interval_ms` is optional. Omitting it means the daemon polls as fast as the bus
allows, distributing bus time across all active subscriptions.

**Response:**
```json
{"jsonrpc":"2.0","id":13,"result":{"subscription_id":"sub_001"}}
```

Any read command may be subscribed. Examples:
```json
{"command":"read",       "params":{"name":"coolant_temp"},  "interval_ms":1000}
{"command":"read_dtcs",  "params":{},                       "interval_ms":5000}
{"command":"test_results","params":{},                      "interval_ms":60000}
{"command":"readiness",  "params":{},                       "interval_ms":30000}
```

Write commands (`clear_dtcs`) cannot be subscribed — the daemon returns error `-32007`.

---

### `stream_unsubscribe`

Cancel a subscription by its ID.

**Request:**
```json
{"jsonrpc":"2.0","id":14,"method":"stream_unsubscribe","params":{"subscription_id":"sub_001"}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":14,"result":{"ok":true}}
```

---

### `request`

Raw escape hatch. Sends a mode and data bytes directly to the vehicle and returns the
raw response. Intended for advanced consumers and manufacturer plugin use. The consumer
sees raw hex bytes — the abstraction is intentionally broken here.

**Request:**
```json
{"jsonrpc":"2.0","id":15,"method":"request","params":{"mode":"01","data":"0C"}}
```

**Response:**
```json
{"jsonrpc":"2.0","id":15,"result":{
    "ecu":      "Engine",
    "response": "410C1AF0"
}}
```

---

## Stream Channel — Event Reference

The stream channel is push-only from the daemon. The consumer never sends messages on
this connection after the `open` handshake. All messages are JSON-RPC notifications
(no `id` field).

---

### `stream_data`

A subscribed command fired at its cadence. Carries the full result of the subscribed
command plus the subscription ID and the timestamp of the poll.

```json
{"jsonrpc":"2.0","method":"stream_data","params":{
    "subscription_id": "sub_001",
    "command":         "read",
    "timestamp":       "2026-06-18T12:00:00.123Z",
    "result": {
        "name":  "rpm",
        "value": 2350,
        "unit":  "rpm"
    }
}}
```

The `result` shape matches the response of the subscribed command exactly.

---

### `vehicle_connected`

Autonomous event. A vehicle was detected, probed, and interrogated successfully.
Sent to all connected stream consumers.

```json
{"jsonrpc":"2.0","method":"vehicle_connected","params":{
    "vin":       "1FTEW1EG7GF...",
    "make":      "Ford",
    "model":     "F-150",
    "year":      2016,
    "protocol":  "ISO15765 / CAN 11-bit 500kbps",
    "mil_on":    true,
    "dtc_count": 1
}}
```

---

### `vehicle_lost`

Autonomous event. Vehicle unplugged or battery voltage dropped below threshold.
Sent to all connected stream consumers and to the admin channel consumer.

```json
{"jsonrpc":"2.0","method":"vehicle_lost","params":{
    "reason":     "voltage_dropped",
    "voltage_mv": 200
}}
```

`reason` values: `"voltage_dropped"` / `"device_lost"`.

---

### `device_lost`

Autonomous event. The J2534 device was physically unplugged. All subscriptions are
cancelled. Stream consumers should expect no further `stream_data` events until a
`vehicle_connected` event arrives after the device is reconnected.

```json
{"jsonrpc":"2.0","method":"device_lost","params":{
    "device": "SuperGoose-Plus"
}}
```

---

## Error Codes

Standard JSON-RPC codes:

| Code | Meaning |
|---|---|
| -32700 | Parse error — message is not valid JSON |
| -32600 | Invalid request — missing required JSON-RPC fields |
| -32601 | Method not found |
| -32602 | Invalid params |
| -32603 | Internal error |

Backprobe-defined codes:

| Code | Meaning |
|---|---|
| -32001 | Admin channel already held by another consumer |
| -32002 | No vehicle session — `vehicle_accept` not yet sent |
| -32003 | No device — J2534 device not found |
| -32004 | Vehicle lost — vehicle disconnected mid-command |
| -32005 | Not supported — vehicle does not support this parameter |
| -32006 | Timeout — no reply from vehicle within allowed time |
| -32007 | Subscribe not allowed — command is a write operation |

---

## End-to-End Example

A scan tool opens both channels and streams live RPM while reading DTCs.

**Connection 1 — admin channel:**
```
→ {"jsonrpc":"2.0","id":1,"method":"open","params":{"channel":"admin"}}
← {"jsonrpc":"2.0","id":1,"result":{"ok":true}}
← {"jsonrpc":"2.0","method":"vehicle_offer","params":{"vin":"1FTEW1EG7GF...","make":"Ford","model":"F-150","year":2016,"protocol":"ISO15765 / CAN 11-bit 500kbps","mil_on":true,"dtc_count":1}}
→ {"jsonrpc":"2.0","id":2,"method":"vehicle_accept","params":{}}
← {"jsonrpc":"2.0","id":2,"result":{"ok":true}}
→ {"jsonrpc":"2.0","id":3,"method":"stream_subscribe","params":{"command":"read","params":{"name":"rpm"},"interval_ms":200}}
← {"jsonrpc":"2.0","id":3,"result":{"subscription_id":"sub_001"}}
→ {"jsonrpc":"2.0","id":4,"method":"read_dtcs","params":{}}
← {"jsonrpc":"2.0","id":4,"result":{"dtcs":[{"code":"P0300","type":"stored","description":"Random/Multiple Cylinder Misfire Detected","ecu":"Engine"}]}}
```

**Connection 2 — stream channel (simultaneous):**
```
→ {"jsonrpc":"2.0","id":1,"method":"open","params":{"channel":"stream"}}
← {"jsonrpc":"2.0","id":1,"result":{"ok":true}}
← {"jsonrpc":"2.0","method":"stream_data","params":{"subscription_id":"sub_001","command":"read","timestamp":"2026-06-18T12:00:00.200Z","result":{"name":"rpm","value":2350,"unit":"rpm"}}}
← {"jsonrpc":"2.0","method":"stream_data","params":{"subscription_id":"sub_001","command":"read","timestamp":"2026-06-18T12:00:00.400Z","result":{"name":"rpm","value":2400,"unit":"rpm"}}}
← {"jsonrpc":"2.0","method":"stream_data","params":{"subscription_id":"sub_001","command":"read","timestamp":"2026-06-18T12:00:00.600Z","result":{"name":"rpm","value":2380,"unit":"rpm"}}}
```

RPM keeps flowing on connection 2 while the admin channel handles the DTC query on
connection 1 independently.

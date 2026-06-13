"""SAE J1979 / ISO 15031-5 Service 01 (Mode 01) PID table.

One entry per standard PID: number, name, unit, payload size, and a decode
rule. The table is the single source of truth for both jobs:

  - Census  — name + unit for every supported PID the bitmask walk finds.
  - Live    — decode(payload) turns raw bytes into a scaled value (Phase 2).

decode is a pure function of the payload bytes (A, B, C, D = data[0..3]),
returning the scaled value. It is None where the PID returns a multi-field
structure the spec lists as "multiple values" — we can NAME such a PID for
the census, but we do not invent a single number for it. Those formulas slot
in later without touching anything else.

Values are returned lossless (unrounded); formatting is the consumer's job.
Support-bitmask PIDs (0x00, 0x20, 0x40, ...) are NOT in this table — they are
structural and handled by decode.decode_supported_pids().

Source: SAE J1979 Mode 01, cross-referenced against the community OBD-II PID
reference. Formulas reproduced exactly (e.g. coolant = A-40, rpm = (256A+B)/4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# ─── byte-formula helpers (keep the table readable and consistent) ────────

def _A(d: bytes) -> int:
    return d[0]


def _u16(d: bytes) -> int:
    """16-bit big-endian from A,B."""
    return d[0] * 256 + d[1]


def _s16(d: bytes) -> int:
    """Signed 16-bit big-endian from A,B (two's complement)."""
    v = d[0] * 256 + d[1]
    return v - 0x10000 if v >= 0x8000 else v


def _pct255(d: bytes) -> float:
    """0–100% over a full byte: (100/255)·A. Load, throttle, levels."""
    return d[0] * 100 / 255


def _trim(d: bytes) -> float:
    """Fuel-trim style: (100/128)·A − 100, centered on 0."""
    return d[0] * 100 / 128 - 100


def _temp(d: bytes) -> int:
    """Temperature: A − 40 °C."""
    return d[0] - 40


# Fuel Type enumeration (PID 0x51), SAE J1979 Table.
_FUEL_TYPES: dict[int, str] = {
    0: "Not available", 1: "Gasoline", 2: "Methanol", 3: "Ethanol",
    4: "Diesel", 5: "LPG", 6: "CNG", 7: "Propane", 8: "Electric",
    9: "Bifuel Gasoline/Electric", 10: "Bifuel Gasoline/CNG",
    11: "Bifuel Propane", 12: "Bifuel Methanol", 13: "Bifuel Ethanol",
    14: "Bifuel LPG", 15: "Bifuel CNG/Electric", 16: "Bifuel Diesel/Electric",
    17: "Diesel/Electric", 18: "Diesel/CNG", 19: "Diesel/Hydrogen",
}


# ─── the entry type ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Pid:
    """One Mode 01 PID. decode is None when the value is multi-field."""

    num: int
    name: str
    unit: str
    n_bytes: int
    decode: Callable[[bytes], float | int | str] | None = None


# ─── the table ─────────────────────────────────────────────────────────────
# decode=None  → named for the census, value formula deferred (multi-field
#                or bit-encoded). NOT a gap in coverage — a deliberate "we
#                name it, we don't fake a single number for it."

_PIDS: list[Pid] = [
    # ── group $01–$1F ──────────────────────────────────────────────────
    Pid(0x01, "monitor_status_since_clear", "", 4, None),   # bit-encoded; MIL/DTC via decode_mil_status
    Pid(0x02, "freeze_frame_dtc", "", 2, None),
    Pid(0x03, "fuel_system_status", "", 2, None),           # enumerated
    Pid(0x04, "engine_load", "%", 1, _pct255),
    Pid(0x05, "engine_coolant_temp", "°C", 1, _temp),
    Pid(0x06, "short_term_fuel_trim_b1", "%", 1, _trim),
    Pid(0x07, "long_term_fuel_trim_b1", "%", 1, _trim),
    Pid(0x08, "short_term_fuel_trim_b2", "%", 1, _trim),
    Pid(0x09, "long_term_fuel_trim_b2", "%", 1, _trim),
    Pid(0x0A, "fuel_pressure", "kPa", 1, lambda d: d[0] * 3),
    Pid(0x0B, "intake_manifold_pressure", "kPa", 1, _A),
    Pid(0x0C, "engine_rpm", "rpm", 2, lambda d: _u16(d) / 4),
    Pid(0x0D, "vehicle_speed", "km/h", 1, _A),
    Pid(0x0E, "timing_advance", "° before TDC", 1, lambda d: d[0] / 2 - 64),
    Pid(0x0F, "intake_air_temp", "°C", 1, _temp),
    Pid(0x10, "maf_air_flow", "g/s", 2, lambda d: _u16(d) / 100),
    Pid(0x11, "throttle_position", "%", 1, _pct255),
    Pid(0x12, "commanded_secondary_air", "", 1, None),      # enumerated
    Pid(0x13, "oxygen_sensors_present_2banks", "", 1, None),  # bit-encoded
    Pid(0x14, "o2_sensor_1", "V", 2, None),                 # voltage + STFT (multi)
    Pid(0x15, "o2_sensor_2", "V", 2, None),
    Pid(0x16, "o2_sensor_3", "V", 2, None),
    Pid(0x17, "o2_sensor_4", "V", 2, None),
    Pid(0x18, "o2_sensor_5", "V", 2, None),
    Pid(0x19, "o2_sensor_6", "V", 2, None),
    Pid(0x1A, "o2_sensor_7", "V", 2, None),
    Pid(0x1B, "o2_sensor_8", "V", 2, None),
    Pid(0x1C, "obd_standards", "", 1, None),                # enumerated
    Pid(0x1D, "oxygen_sensors_present_4banks", "", 1, None),  # bit-encoded
    Pid(0x1E, "auxiliary_input_status", "", 1, None),       # bit-encoded
    Pid(0x1F, "runtime_since_start", "s", 2, _u16),
    # ── group $21–$3F ──────────────────────────────────────────────────
    Pid(0x21, "distance_with_mil_on", "km", 2, _u16),
    Pid(0x22, "fuel_rail_pressure_relative", "kPa", 2, lambda d: _u16(d) * 0.079),
    Pid(0x23, "fuel_rail_gauge_pressure", "kPa", 2, lambda d: _u16(d) * 10),
    Pid(0x24, "o2_sensor_1_lambda", "ratio", 4, None),
    Pid(0x25, "o2_sensor_2_lambda", "ratio", 4, None),
    Pid(0x26, "o2_sensor_3_lambda", "ratio", 4, None),
    Pid(0x27, "o2_sensor_4_lambda", "ratio", 4, None),
    Pid(0x28, "o2_sensor_5_lambda", "ratio", 4, None),
    Pid(0x29, "o2_sensor_6_lambda", "ratio", 4, None),
    Pid(0x2A, "o2_sensor_7_lambda", "ratio", 4, None),
    Pid(0x2B, "o2_sensor_8_lambda", "ratio", 4, None),
    Pid(0x2C, "commanded_egr", "%", 1, _pct255),
    Pid(0x2D, "egr_error", "%", 1, _trim),
    Pid(0x2E, "commanded_evap_purge", "%", 1, _pct255),
    Pid(0x2F, "fuel_tank_level", "%", 1, _pct255),
    Pid(0x30, "warmups_since_clear", "", 1, _A),
    Pid(0x31, "distance_since_clear", "km", 2, _u16),
    Pid(0x32, "evap_vapor_pressure", "Pa", 2, lambda d: _s16(d) / 4),
    Pid(0x33, "barometric_pressure", "kPa", 1, _A),
    Pid(0x34, "o2_sensor_1_lambda_current", "ratio", 4, None),
    Pid(0x35, "o2_sensor_2_lambda_current", "ratio", 4, None),
    Pid(0x36, "o2_sensor_3_lambda_current", "ratio", 4, None),
    Pid(0x37, "o2_sensor_4_lambda_current", "ratio", 4, None),
    Pid(0x38, "o2_sensor_5_lambda_current", "ratio", 4, None),
    Pid(0x39, "o2_sensor_6_lambda_current", "ratio", 4, None),
    Pid(0x3A, "o2_sensor_7_lambda_current", "ratio", 4, None),
    Pid(0x3B, "o2_sensor_8_lambda_current", "ratio", 4, None),
    Pid(0x3C, "catalyst_temp_b1s1", "°C", 2, lambda d: _u16(d) / 10 - 40),
    Pid(0x3D, "catalyst_temp_b2s1", "°C", 2, lambda d: _u16(d) / 10 - 40),
    Pid(0x3E, "catalyst_temp_b1s2", "°C", 2, lambda d: _u16(d) / 10 - 40),
    Pid(0x3F, "catalyst_temp_b2s2", "°C", 2, lambda d: _u16(d) / 10 - 40),
    # ── group $41–$5F ──────────────────────────────────────────────────
    Pid(0x41, "monitor_status_drive_cycle", "", 4, None),   # bit-encoded
    Pid(0x42, "control_module_voltage", "V", 2, lambda d: _u16(d) / 1000),
    Pid(0x43, "absolute_load", "%", 2, lambda d: _u16(d) * 100 / 255),
    Pid(0x44, "commanded_afr", "ratio", 2, lambda d: _u16(d) * 2 / 65536),
    Pid(0x45, "relative_throttle_position", "%", 1, _pct255),
    Pid(0x46, "ambient_air_temp", "°C", 1, _temp),
    Pid(0x47, "absolute_throttle_position_b", "%", 1, _pct255),
    Pid(0x48, "absolute_throttle_position_c", "%", 1, _pct255),
    Pid(0x49, "accelerator_pedal_position_d", "%", 1, _pct255),
    Pid(0x4A, "accelerator_pedal_position_e", "%", 1, _pct255),
    Pid(0x4B, "accelerator_pedal_position_f", "%", 1, _pct255),
    Pid(0x4C, "commanded_throttle_actuator", "%", 1, _pct255),
    Pid(0x4D, "time_with_mil_on", "min", 2, _u16),
    Pid(0x4E, "time_since_codes_cleared", "min", 2, _u16),
    Pid(0x4F, "max_values_afr_o2_map", "", 4, None),        # multi
    Pid(0x50, "max_maf_air_flow", "g/s", 4, lambda d: d[0] * 10),
    Pid(0x51, "fuel_type", "", 1, lambda d: _FUEL_TYPES.get(d[0], f"Type {d[0]}")),
    Pid(0x52, "ethanol_fuel_percent", "%", 1, _pct255),
    Pid(0x53, "absolute_evap_vapor_pressure", "kPa", 2, lambda d: _u16(d) / 200),
    Pid(0x54, "evap_vapor_pressure_abs", "Pa", 2, lambda d: _s16(d)),
    Pid(0x55, "short_term_secondary_o2_trim_b1b3", "%", 2, None),   # multi
    Pid(0x56, "long_term_secondary_o2_trim_b1b3", "%", 2, None),
    Pid(0x57, "short_term_secondary_o2_trim_b2b4", "%", 2, None),
    Pid(0x58, "long_term_secondary_o2_trim_b2b4", "%", 2, None),
    Pid(0x59, "fuel_rail_absolute_pressure", "kPa", 2, lambda d: _u16(d) * 10),
    Pid(0x5A, "relative_accelerator_pedal_position", "%", 1, _pct255),
    Pid(0x5B, "hybrid_battery_remaining_life", "%", 1, _pct255),
    Pid(0x5C, "engine_oil_temp", "°C", 1, _temp),
    Pid(0x5D, "fuel_injection_timing", "°", 2, lambda d: _u16(d) / 128 - 210),
    Pid(0x5E, "engine_fuel_rate", "L/h", 2, lambda d: _u16(d) / 20),
    Pid(0x5F, "emission_requirements", "", 1, None),        # bit-encoded
    # ── group $61–$7F (torque + diesel/turbo, mostly multi-field) ──────
    Pid(0x61, "demand_engine_torque", "%", 1, lambda d: d[0] - 125),
    Pid(0x62, "actual_engine_torque", "%", 1, lambda d: d[0] - 125),
    Pid(0x63, "engine_reference_torque", "N·m", 2, _u16),
    Pid(0x64, "engine_percent_torque_data", "%", 5, None),  # multi
    Pid(0x65, "auxiliary_io_supported", "", 2, None),
    Pid(0x66, "mass_air_flow_sensor", "g/s", 5, None),
    Pid(0x67, "engine_coolant_temp_sensors", "°C", 3, None),
    Pid(0x68, "intake_air_temp_sensors", "°C", 3, None),
    Pid(0x69, "commanded_egr_and_error", "", 7, None),
    Pid(0x6A, "commanded_diesel_air_flow", "", 5, None),
    Pid(0x6B, "exhaust_gas_recirculation_temp", "°C", 5, None),
    Pid(0x6C, "commanded_throttle_actuator_control", "", 5, None),
    Pid(0x6D, "fuel_pressure_control_system", "", 11, None),
    Pid(0x6E, "injection_pressure_control_system", "", 9, None),
    Pid(0x6F, "turbo_compressor_inlet_pressure", "", 3, None),
    Pid(0x70, "boost_pressure_control", "kPa", 10, None),
    Pid(0x71, "vgt_control", "", 6, None),
    Pid(0x72, "wastegate_control", "", 5, None),
    Pid(0x73, "exhaust_pressure", "", 5, None),
    Pid(0x74, "turbocharger_rpm", "rpm", 5, None),
    Pid(0x75, "turbocharger_temp_a", "°C", 7, None),
    Pid(0x76, "turbocharger_temp_b", "°C", 7, None),
    Pid(0x77, "charge_air_cooler_temp", "°C", 5, None),
    Pid(0x78, "egt_bank_1", "°C", 9, None),
    Pid(0x79, "egt_bank_2", "°C", 9, None),
    Pid(0x7A, "dpf_differential_pressure_b1", "", 7, None),
    Pid(0x7B, "dpf_b2", "", 7, None),
    Pid(0x7C, "dpf_temp", "°C", 9, lambda d: _u16(d) / 10 - 40),
    Pid(0x7D, "nox_nte_control_area_status", "", 1, None),
    Pid(0x7E, "pm_nte_control_area_status", "", 1, None),
    Pid(0x7F, "engine_run_time", "s", 13, None),
    # ── group $81–$A0 (heavy-duty / WWH-OBD, multi-field) ──────────────
    Pid(0x83, "nox_sensor", "", 9, None),
    Pid(0x84, "manifold_surface_temp", "°C", 1, _temp),
    Pid(0x85, "nox_reagent_system", "%", 10, None),
    Pid(0x86, "particulate_matter_sensor", "", 5, None),
    Pid(0x87, "intake_manifold_abs_pressure", "", 5, None),
    Pid(0x8D, "throttle_position_g", "%", 1, _pct255),
    Pid(0x8E, "engine_friction_torque", "%", 1, lambda d: d[0] - 125),
    # ── group $A1–$C0 ──────────────────────────────────────────────────
    Pid(0xA2, "cylinder_fuel_rate", "mg/stroke", 2, lambda d: _u16(d) / 32),
    Pid(0xA4, "transmission_actual_gear", "ratio", 4, None),
    Pid(0xA6, "odometer", "km", 4, lambda d: (d[0] << 24 | d[1] << 16 | d[2] << 8 | d[3]) / 10),
    # ── group $C1–$E0 ──────────────────────────────────────────────────
    Pid(0xC3, "fuel_level_input_ab", "%", 2, None),
    Pid(0xC7, "distance_since_reflash", "km", 2, _u16),
]


# Number → entry. The lookup the census and live decode both use.
PIDS: dict[int, Pid] = {p.num: p for p in _PIDS}


def name_of(pid: int) -> str | None:
    """Human name for a PID number, or None if not a known standard PID."""
    entry = PIDS.get(pid)
    return entry.name if entry else None

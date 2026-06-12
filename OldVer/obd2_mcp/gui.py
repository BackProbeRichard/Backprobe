"""Standalone GUI scan tool — OBD2 AI Tool.

A customtkinter-based desktop application.  Works completely offline; no
Claude subscription or network connection required.  Wraps the same
transport / protocol layer used by the MCP server.

Run with:
    obd2-gui
    OBD2_TRANSPORT=virtual obd2-gui
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import customtkinter as ctk

from obd2_mcp.config import J2534_KNOWN_DEVICES, settings
from obd2_mcp.instance_lock import acquire_instance_lock
from obd2_mcp.protocols.kwp2000 import KWP2000Protocol
from obd2_mcp.protocols.obd2 import OBD2Protocol
from obd2_mcp.protocols.uds import UDSProtocol
from obd2_mcp.session_log import (
    install_crash_handler,
    instrument_transport,
    log_error,
    log_event,
    log_system_info,
    set_source,
)
from obd2_mcp.transport.base import BaseTransport

# ── appearance ────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ── SAE J1979 Mode 01 PID name lookup ────────────────────────────────────────
_PID_NAMES: dict[int, str] = {
    0x01: "Monitor Status",
    0x02: "Freeze Frame DTC",
    0x03: "Fuel System Status",
    0x04: "Engine Load",
    0x05: "Coolant Temperature",
    0x06: "Short-term Fuel Trim B1",
    0x07: "Long-term Fuel Trim B1",
    0x08: "Short-term Fuel Trim B2",
    0x09: "Long-term Fuel Trim B2",
    0x0A: "Fuel Rail Pressure",
    0x0B: "MAP Pressure",
    0x0C: "Engine RPM",
    0x0D: "Vehicle Speed",
    0x0E: "Timing Advance",
    0x0F: "Intake Air Temp",
    0x10: "MAF Air Flow",
    0x11: "Throttle Position",
    0x12: "Secondary Air Status",
    0x13: "O2 Sensors Present",
    0x1C: "OBD Standard",
    0x1F: "Engine Run Time",
    0x21: "Distance with MIL On",
    0x2C: "EGR Command",
    0x2D: "EGR Error",
    0x2E: "Evap Purge",
    0x2F: "Fuel Level",
    0x30: "Warm-ups Since Clear",
    0x31: "Distance Since Clear",
    0x33: "Barometric Pressure",
    0x3C: "Cat Temp B1S1",
    0x3D: "Cat Temp B2S1",
    0x42: "Control Module Voltage",
    0x43: "Absolute Load",
    0x45: "Relative Throttle",
    0x46: "Ambient Air Temp",
    0x47: "Throttle Position B",
    0x49: "Accel Pedal D",
    0x4A: "Accel Pedal E",
    0x4C: "Commanded Throttle",
    0x51: "Fuel Type",
    0x5C: "Oil Temperature",
    0x5E: "Fuel Rate",
    0x61: "Driver Demand Torque",
    0x62: "Actual Engine Torque",
    0x63: "Engine Reference Torque",
}

# Default PIDs shown before discovery completes
_DEFAULT_PIDS: list[tuple[int, str]] = [
    (0x0C, "Engine RPM"),
    (0x0D, "Vehicle Speed"),
    (0x05, "Coolant Temperature"),
    (0x04, "Engine Load"),
    (0x11, "Throttle Position"),
    (0x0B, "MAP Pressure"),
    (0x0F, "Intake Air Temp"),
    (0x10, "MAF Air Flow"),
    (0x0E, "Timing Advance"),
    (0x1F, "Engine Run Time"),
    (0x2F, "Fuel Level"),
    (0x51, "Fuel Type"),
]

# PIDs to request for freeze frame (diagnostic snapshot)
_FREEZE_FRAME_PIDS: list[int] = [
    0x0C, 0x0D, 0x05, 0x04, 0x11, 0x0B, 0x0F, 0x10, 0x0E, 0x1F, 0x2F,
]

# UDS DIDs shown in Vehicle Info tab
ECU_DIDS: list[tuple[int, str]] = [
    (0xF190, "VIN (UDS)"),
    (0xF187, "Part Number"),
    (0xF80A, "Calibration ID (UDS)"),
    (0xF18C, "ECU Serial Number"),
    (0xF18B, "Data Release Date"),
    (0xF197, "System Supplier ID"),
]

_BTN_RED = ("#c0392b", "#922b21")
_CLR_MUTED = ("gray60", "gray50")   # de-emphasized: stale/held or culled live values
# Default CTkLabel text color — used to restore a value cell after it was dimmed.
_CLR_VALUE = ctk.ThemeManager.theme["CTkLabel"]["text_color"]
# Status-bar colors: disconnected / connected-no-vehicle / vehicle present.
_CLR_STATUS_OFF = ("orange3", "orange")     # disconnected
_CLR_STATUS_OK  = ("green4", "#2ecc71")     # connected + vehicle present
# (connected but no vehicle uses _CLR_VALUE — default white)

# Common OBD2 DTC descriptions — codes not listed here show "—"
_DTC_DESC: dict[str, str] = {
    "P0100": "MAF Sensor Circuit",           "P0101": "MAF Sensor Range/Performance",
    "P0102": "MAF Sensor Circuit Low",       "P0103": "MAF Sensor Circuit High",
    "P0110": "IAT Sensor Circuit",           "P0112": "IAT Sensor Circuit Low",
    "P0113": "IAT Sensor Circuit High",      "P0115": "ECT Sensor Circuit",
    "P0117": "ECT Sensor Circuit Low",       "P0118": "ECT Sensor Circuit High",
    "P0120": "Throttle Position Circuit",    "P0121": "TPS Range/Performance",
    "P0122": "TPS Circuit Low",              "P0123": "TPS Circuit High",
    "P0130": "O2 Sensor B1S1",              "P0131": "O2 Sensor B1S1 Low Voltage",
    "P0132": "O2 Sensor B1S1 High Voltage", "P0133": "O2 Sensor B1S1 Slow Response",
    "P0135": "O2 Sensor B1S1 Heater",       "P0136": "O2 Sensor B1S2",
    "P0137": "O2 Sensor B1S2 Low Voltage",  "P0138": "O2 Sensor B1S2 High Voltage",
    "P0141": "O2 Sensor B1S2 Heater",       "P0171": "System Too Lean (Bank 1)",
    "P0172": "System Too Rich (Bank 1)",     "P0174": "System Too Lean (Bank 2)",
    "P0175": "System Too Rich (Bank 2)",     "P0300": "Random/Multiple Misfire",
    "P0301": "Cylinder 1 Misfire",           "P0302": "Cylinder 2 Misfire",
    "P0303": "Cylinder 3 Misfire",           "P0304": "Cylinder 4 Misfire",
    "P0305": "Cylinder 5 Misfire",           "P0306": "Cylinder 6 Misfire",
    "P0307": "Cylinder 7 Misfire",           "P0308": "Cylinder 8 Misfire",
    "P0325": "Knock Sensor 1",               "P0327": "Knock Sensor 1 Low",
    "P0335": "CKP Sensor A Circuit",         "P0340": "CMP Sensor A Circuit",
    "P0400": "EGR Flow",                     "P0401": "EGR Flow Insufficient",
    "P0402": "EGR Flow Excessive",           "P0420": "Catalyst Efficiency Low B1",
    "P0430": "Catalyst Efficiency Low B2",   "P0440": "EVAP System",
    "P0441": "EVAP Incorrect Purge Flow",    "P0442": "EVAP Small Leak",
    "P0455": "EVAP Large Leak",              "P0456": "EVAP Very Small Leak",
    "P0500": "Vehicle Speed Sensor",         "P0505": "Idle Control System",
    "P0506": "Idle Control Low RPM",         "P0507": "Idle Control High RPM",
    "P0560": "System Voltage",               "P0562": "System Voltage Low",
    "P0563": "System Voltage High",          "P0601": "ECM Memory Checksum",
    "P0606": "ECM/PCM Processor",            "P0700": "TCM Malfunction",
    "P0730": "Incorrect Gear Ratio",         "P0740": "TCC Circuit",
    "P0741": "TCC Stuck Off",                "P0742": "TCC Stuck On",
    "P0750": "Shift Solenoid A",             "P0755": "Shift Solenoid B",
    "P1000": "OBD Readiness Not Complete",
}


# ─────────────────────────────────────────────────────────────────────────────
# Background asyncio runner
# ─────────────────────────────────────────────────────────────────────────────

class _AsyncRunner:
    """Runs a dedicated asyncio event loop on a daemon thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="obd2-async"
        )
        self._thread.start()

    def submit(self, coro) -> "asyncio.Future[Any]":
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)


# ─────────────────────────────────────────────────────────────────────────────
# Main application window
# ─────────────────────────────────────────────────────────────────────────────

class OBD2App(ctk.CTk):

    def __init__(self) -> None:
        super().__init__()
        self.title("OBD2 Scan Tool")
        self.geometry("1020x720")
        self.minsize(820, 580)

        self._runner = _AsyncRunner()
        self._transport: BaseTransport | None = None
        self._obd2: OBD2Protocol | None = None
        self._uds: UDSProtocol | None = None
        self._kwp2000: KWP2000Protocol | None = None
        self._polling = False
        self._poll_job: str | None = None

        # Adaptive polling state
        self._auto_rate = False
        self._poll_start_time: float = 0.0
        self._smoothed_poll_ms: float = 500.0
        # Manual refresh interval (ms). Only updated when the user commits the
        # entry via Enter or the ✓ button — never live as they type.
        self._applied_interval_ms: int = 500

        # Active PIDs for live data (updated after Mode 01 discovery)
        self._active_pids: list[tuple[int, str]] = list(_DEFAULT_PIDS)
        # Live-data resilience: cull chronically-unanswered PIDs and keep the
        # last good value so the grid doesn't flicker to hex/N/A on a stray miss.
        self._pid_fail_counts: dict[int, int] = {}
        self._pid_culled: set[int] = set()
        self._pid_last_value: dict[int, tuple[str, str]] = {}

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._status_var = ctk.StringVar(value="● Disconnected")
        self._status_lbl = ctk.CTkLabel(
            self,
            textvariable=self._status_var,
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=_CLR_STATUS_OFF,
        )
        self._status_lbl.pack(side="bottom", fill="x", padx=12, pady=4)

        tabs = ctk.CTkTabview(self, anchor="nw")
        tabs.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self._tabs = tabs

        for name in ("Connection", "Live Data", "DTCs", "Freeze Frame", "Monitors", "Vehicle Info"):
            tabs.add(name)

        self._build_connection_tab()
        self._build_live_data_tab()
        self._build_dtc_tab()
        self._build_freeze_frame_tab()
        self._build_monitors_tab()
        self._build_vehicle_info_tab()

    # ── Connection tab ────────────────────────────────────────────────────────

    def _build_connection_tab(self) -> None:
        tab = self._tabs.tab("Connection")
        tab.columnconfigure(1, weight=1)

        ctk.CTkLabel(tab, text="Transport:").grid(
            row=0, column=0, sticky="e", padx=12, pady=10
        )
        self._transport_var = ctk.StringVar(value="virtual")
        ctk.CTkOptionMenu(
            tab,
            values=["virtual", "elm327", "j2534", "kline", "doip"],
            variable=self._transport_var,
            command=self._on_transport_changed,
            width=200,
        ).grid(row=0, column=1, sticky="w", padx=10, pady=10)

        # Contextual widgets ──────────────────────────────────────────────────

        self._port_label = ctk.CTkLabel(tab, text="Serial Port:")
        self._port_entry = ctk.CTkEntry(
            tab, placeholder_text="COM3  or  /dev/ttyUSB0", width=260
        )

        self._dll_label = ctk.CTkLabel(tab, text="J2534 Device:")
        self._dll_var = ctk.StringVar(value=list(J2534_KNOWN_DEVICES.keys())[0])
        self._dll_menu = ctk.CTkOptionMenu(
            tab,
            values=list(J2534_KNOWN_DEVICES.keys()) + ["custom…"],
            variable=self._dll_var,
            width=200,
        )
        self._dll_custom_label = ctk.CTkLabel(tab, text="DLL Path:")
        self._dll_custom_entry = ctk.CTkEntry(
            tab, placeholder_text=r"C:\…\J2534.dll", width=380
        )
        self._can_fd_var = ctk.BooleanVar(value=False)
        self._can_fd_check = ctk.CTkCheckBox(
            tab, text="CAN FD  (2 Mbps data phase)", variable=self._can_fd_var
        )

        self._kline_dll_label = ctk.CTkLabel(tab, text="J2534 Device:")
        self._kline_dll_var = ctk.StringVar(value=list(J2534_KNOWN_DEVICES.keys())[0])
        self._kline_dll_menu = ctk.CTkOptionMenu(
            tab,
            values=list(J2534_KNOWN_DEVICES.keys()) + ["custom…"],
            variable=self._kline_dll_var,
            width=200,
        )
        self._kline_proto_label = ctk.CTkLabel(tab, text="Protocol:")
        self._kline_proto_var = ctk.StringVar(value="iso14230")
        self._kline_proto_menu = ctk.CTkOptionMenu(
            tab,
            values=["iso14230", "iso9141"],
            variable=self._kline_proto_var,
            width=180,
        )

        self._doip_host_label = ctk.CTkLabel(tab, text="Gateway IP:")
        self._doip_host_entry = ctk.CTkEntry(tab, placeholder_text="169.254.0.10", width=200)
        self._doip_port_label = ctk.CTkLabel(tab, text="Port:")
        self._doip_port_entry = ctk.CTkEntry(tab, placeholder_text="13400", width=90)
        self._doip_src_label  = ctk.CTkLabel(tab, text="Tester addr (hex):")
        self._doip_src_entry  = ctk.CTkEntry(tab, placeholder_text="0E00", width=90)
        self._doip_tgt_label  = ctk.CTkLabel(tab, text="ECU addr (hex):")
        self._doip_tgt_entry  = ctk.CTkEntry(tab, placeholder_text="0010", width=90)

        self._on_transport_changed("virtual")

        # Connect / Disconnect ────────────────────────────────────────────────
        btn_row = ctk.CTkFrame(tab, fg_color="transparent")
        btn_row.grid(row=10, column=0, columnspan=2, sticky="w", padx=12, pady=16)

        self._connect_btn = ctk.CTkButton(
            btn_row, text="Connect", command=self._connect, width=130
        )
        self._connect_btn.pack(side="left", padx=(0, 10))

        self._disconnect_btn = ctk.CTkButton(
            btn_row,
            text="Disconnect",
            command=self._disconnect,
            fg_color="gray40",
            hover_color="gray30",
            width=130,
            state="disabled",
        )
        self._disconnect_btn.pack(side="left")

        # Device info ─────────────────────────────────────────────────────────
        ctk.CTkLabel(tab, text="Device Info:").grid(
            row=11, column=0, sticky="ne", padx=12, pady=6
        )
        self._device_info_box = ctk.CTkTextbox(tab, height=80, state="disabled")
        self._device_info_box.grid(row=11, column=1, sticky="ew", padx=10, pady=6)

        tab.rowconfigure(11, weight=1)

    def _on_transport_changed(self, value: str) -> None:
        _all_contextual = (
            self._port_label, self._port_entry,
            self._dll_label, self._dll_menu,
            self._dll_custom_label, self._dll_custom_entry, self._can_fd_check,
            self._kline_dll_label, self._kline_dll_menu,
            self._kline_proto_label, self._kline_proto_menu,
            self._doip_host_label, self._doip_host_entry,
            self._doip_port_label, self._doip_port_entry,
            self._doip_src_label, self._doip_src_entry,
            self._doip_tgt_label, self._doip_tgt_entry,
        )
        for w in _all_contextual:
            try:
                w.grid_forget()
            except Exception:
                pass

        if value == "elm327":
            self._port_label.grid(row=1, column=0, sticky="e", padx=12, pady=8)
            self._port_entry.grid(row=1, column=1, sticky="w", padx=10, pady=8)
        elif value == "j2534":
            self._dll_label.grid(row=1, column=0, sticky="e", padx=12, pady=8)
            self._dll_menu.grid(row=1, column=1, sticky="w", padx=10, pady=8)
            self._dll_custom_label.grid(row=2, column=0, sticky="e", padx=12, pady=4)
            self._dll_custom_entry.grid(row=2, column=1, sticky="w", padx=10, pady=4)
            self._can_fd_check.grid(row=3, column=1, sticky="w", padx=10, pady=4)
        elif value == "kline":
            self._kline_dll_label.grid(row=1, column=0, sticky="e", padx=12, pady=8)
            self._kline_dll_menu.grid(row=1, column=1, sticky="w", padx=10, pady=8)
            self._kline_proto_label.grid(row=2, column=0, sticky="e", padx=12, pady=8)
            self._kline_proto_menu.grid(row=2, column=1, sticky="w", padx=10, pady=8)
        elif value == "doip":
            self._doip_host_label.grid(row=1, column=0, sticky="e", padx=12, pady=8)
            self._doip_host_entry.grid(row=1, column=1, sticky="w", padx=10, pady=8)
            self._doip_port_label.grid(row=2, column=0, sticky="e", padx=12, pady=6)
            self._doip_port_entry.grid(row=2, column=1, sticky="w", padx=10, pady=6)
            self._doip_src_label.grid(row=3, column=0, sticky="e", padx=12, pady=6)
            self._doip_src_entry.grid(row=3, column=1, sticky="w", padx=10, pady=6)
            self._doip_tgt_label.grid(row=4, column=0, sticky="e", padx=12, pady=6)
            self._doip_tgt_entry.grid(row=4, column=1, sticky="w", padx=10, pady=6)

    # ── Shared table helper ───────────────────────────────────────────────────

    def _sync_header_to_scroll(
        self, header: ctk.CTkFrame, scroll: ctk.CTkScrollableFrame
    ) -> None:
        """Align the frozen header to the CTkScrollableFrame content area.

        CTkScrollableFrame places its canvas with border_spacing padding on each
        side (corner_radius + border_width), so the content area is narrower than
        the outer frame by sb_width + 2*border_spacing.  Measure all three values
        directly from rendered widget widths so this works at any DPI / scaling.
        """
        sb_width = scroll._scrollbar.winfo_width()
        if sb_width < 2:
            header.after(50, lambda: self._sync_header_to_scroll(header, scroll))
            return
        pf_w = scroll._parent_frame.winfo_width()
        cv_w = scroll._parent_canvas.winfo_width()
        bs = max(0, (pf_w - cv_w - sb_width) // 2)
        header.pack_configure(padx=(10 + bs, 10 + sb_width + bs))

    # ── Live Data tab ─────────────────────────────────────────────────────────

    def _build_live_data_tab(self) -> None:
        tab = self._tabs.tab("Live Data")

        toolbar = ctk.CTkFrame(tab, fg_color="transparent")
        toolbar.pack(fill="x", padx=10, pady=(10, 4))

        self._poll_btn = ctk.CTkButton(
            toolbar,
            text="▶  Start Polling",
            command=self._toggle_polling,
            width=150,
            state="disabled",
        )
        self._poll_btn.pack(side="left")

        ctk.CTkLabel(toolbar, text="Refresh (s):").pack(side="left", padx=(20, 6))
        self._interval_var = ctk.StringVar(value="0.5")
        self._interval_entry = ctk.CTkEntry(toolbar, textvariable=self._interval_var, width=55)
        self._interval_entry.pack(side="left")
        # Interval is committed only on Enter or the ✓ button, not while typing.
        self._interval_entry.bind("<Return>", lambda _e: self._apply_interval())
        self._interval_apply_btn = ctk.CTkButton(
            toolbar, text="✓", command=self._apply_interval, width=28,
        )
        self._interval_apply_btn.pack(side="left", padx=(4, 0))

        self._auto_var = ctk.BooleanVar(value=False)
        self._auto_check = ctk.CTkCheckBox(
            toolbar, text="Auto", variable=self._auto_var,
            command=self._on_auto_toggled, width=60,
        )
        self._auto_check.pack(side="left", padx=(10, 0))

        self._rate_lbl = ctk.CTkLabel(
            toolbar, text="", text_color="gray60",
            font=ctk.CTkFont(size=11), width=100, anchor="w",
        )
        self._rate_lbl.pack(side="left", padx=(8, 0))

        self._discover_lbl = ctk.CTkLabel(
            toolbar, text="", text_color="gray50",
            font=ctk.CTkFont(size=11), anchor="w",
        )
        self._discover_lbl.pack(side="left", padx=(12, 0))

        # Table header (frozen) ───────────────────────────────────────────────
        live_hdr = ctk.CTkFrame(tab, fg_color=("gray85", "gray20"), corner_radius=6)
        live_hdr.pack(fill="x", padx=10, pady=(8, 0))
        live_hdr.columnconfigure(0, weight=3, minsize=220)
        live_hdr.columnconfigure(1, weight=2, minsize=140)
        live_hdr.columnconfigure(2, weight=1, minsize=80)
        for col, text in enumerate(("Parameter", "Value", "Unit")):
            ctk.CTkLabel(
                live_hdr, text=text, font=ctk.CTkFont(weight="bold"), anchor="w"
            ).grid(row=0, column=col, sticky="w", padx=10, pady=4)

        # Scrollable rows ─────────────────────────────────────────────────────
        self._live_scroll = ctk.CTkScrollableFrame(tab)
        self._live_scroll.pack(fill="both", expand=True, padx=10, pady=(2, 6))
        self._live_scroll.columnconfigure(0, weight=3, minsize=220)
        self._live_scroll.columnconfigure(1, weight=2, minsize=140)
        self._live_scroll.columnconfigure(2, weight=1, minsize=80)
        live_hdr.after(0, lambda: self._sync_header_to_scroll(live_hdr, self._live_scroll))

        self._pid_rows: dict[int, tuple[ctk.CTkLabel, ctk.CTkLabel, ctk.CTkLabel]] = {}
        self._build_live_data_rows(self._active_pids)

    def _build_live_data_rows(self, pids: list[tuple[int, str]]) -> None:
        """Clear and rebuild the live data table rows."""
        for widget in self._live_scroll.winfo_children():
            widget.destroy()
        self._pid_rows.clear()

        for row_idx, (pid, name) in enumerate(pids):
            bg = ("gray92", "gray17") if row_idx % 2 == 0 else ("gray88", "gray14")
            for col in range(3):
                ctk.CTkFrame(
                    self._live_scroll, fg_color=bg, corner_radius=0, height=28
                ).grid(row=row_idx, column=col, sticky="nsew", padx=0, pady=0)
            name_lbl = ctk.CTkLabel(
                self._live_scroll,
                text=f"0x{pid:02X}  {name}",
                anchor="w",
                fg_color=bg,
            )
            val_lbl  = ctk.CTkLabel(self._live_scroll, text="—", anchor="w", fg_color=bg)
            unit_lbl = ctk.CTkLabel(self._live_scroll, text="",  anchor="w", fg_color=bg)
            name_lbl.grid(row=row_idx, column=0, sticky="w", padx=10, pady=3)
            val_lbl.grid(row=row_idx,  column=1, sticky="w", padx=10, pady=3)
            unit_lbl.grid(row=row_idx, column=2, sticky="w", padx=10, pady=3)
            self._pid_rows[pid] = (name_lbl, val_lbl, unit_lbl)

    # ── DTCs tab ──────────────────────────────────────────────────────────────

    _DTC_COLS = [
        ("DTC",         1,  90, "w"),
        ("Description", 4, 200, "w"),
        ("Status",      1,  90, "e"),
    ]

    def _build_dtc_tab(self) -> None:
        tab = self._tabs.tab("DTCs")

        toolbar = ctk.CTkFrame(tab, fg_color="transparent")
        toolbar.pack(fill="x", padx=10, pady=(10, 4))

        self._read_dtc_btn = ctk.CTkButton(
            toolbar, text="Read DTCs", command=self._read_dtcs,
            width=130, state="disabled",
        )
        self._read_dtc_btn.pack(side="left")

        self._clear_dtc_btn = ctk.CTkButton(
            toolbar, text="Clear DTCs", command=self._clear_dtcs,
            fg_color=_BTN_RED[0], hover_color=_BTN_RED[1],
            width=120, state="disabled",
        )
        self._clear_dtc_btn.pack(side="left", padx=(8, 0))

        self._dtc_count_lbl = ctk.CTkLabel(toolbar, text="", anchor="w")
        self._dtc_count_lbl.pack(side="left", padx=12)

        # Table — header row 0 inside scroll frame for guaranteed alignment
        self._dtc_scroll = ctk.CTkScrollableFrame(tab)
        self._dtc_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        for col, (_, weight, minsize, _) in enumerate(self._DTC_COLS):
            self._dtc_scroll.columnconfigure(col, weight=weight, minsize=minsize)

        hdr_bg = ("gray85", "gray20")
        for col in range(len(self._DTC_COLS)):
            ctk.CTkFrame(
                self._dtc_scroll, fg_color=hdr_bg, corner_radius=0, height=30
            ).grid(row=0, column=col, sticky="nsew")
        for col, (text, _, _, anchor) in enumerate(self._DTC_COLS):
            sticky = "e" if anchor == "e" else "w"
            ctk.CTkLabel(
                self._dtc_scroll, text=text,
                font=ctk.CTkFont(weight="bold"), anchor=anchor, fg_color=hdr_bg,
            ).grid(row=0, column=col, sticky=sticky, padx=10, pady=4)

        self._dtc_data_widgets: list = []
        self._stored_dtc_codes: list[str] = []

    # ── Freeze Frame tab ─────────────────────────────────────────────────────

    def _build_freeze_frame_tab(self) -> None:
        tab = self._tabs.tab("Freeze Frame")

        toolbar = ctk.CTkFrame(tab, fg_color="transparent")
        toolbar.pack(fill="x", padx=10, pady=(10, 4))

        ctk.CTkLabel(toolbar, text="Frame #:").pack(side="left")
        self._ff_frame_var = ctk.StringVar(value="0")
        ctk.CTkEntry(
            toolbar, textvariable=self._ff_frame_var, width=46
        ).pack(side="left", padx=(6, 16))

        self._read_ff_btn = ctk.CTkButton(
            toolbar, text="Read Freeze Frame", command=self._read_freeze_frame,
            width=170, state="disabled",
        )
        self._read_ff_btn.pack(side="left")

        self._ff_status_lbl = ctk.CTkLabel(
            toolbar, text="", text_color="gray60", anchor="w"
        )
        self._ff_status_lbl.pack(side="left", padx=12)

        # Header (frozen) ─────────────────────────────────────────────────────
        ff_hdr = ctk.CTkFrame(tab, fg_color=("gray85", "gray20"), corner_radius=6)
        ff_hdr.pack(fill="x", padx=10, pady=(8, 0))
        ff_hdr.columnconfigure(0, weight=3, minsize=220)
        ff_hdr.columnconfigure(1, weight=2, minsize=140)
        ff_hdr.columnconfigure(2, weight=1, minsize=80)
        for col, text in enumerate(("Parameter", "Value", "Unit")):
            ctk.CTkLabel(
                ff_hdr, text=text, font=ctk.CTkFont(weight="bold"), anchor="w"
            ).grid(row=0, column=col, sticky="w", padx=10, pady=4)

        # Rows ────────────────────────────────────────────────────────────────
        ff_scroll = ctk.CTkScrollableFrame(tab)
        ff_scroll.pack(fill="both", expand=True, padx=10, pady=(2, 6))
        ff_scroll.columnconfigure(0, weight=3, minsize=220)
        ff_scroll.columnconfigure(1, weight=2, minsize=140)
        ff_scroll.columnconfigure(2, weight=1, minsize=80)
        ff_hdr.after(0, lambda: self._sync_header_to_scroll(ff_hdr, ff_scroll))

        self._ff_rows: dict[int, tuple[ctk.CTkLabel, ctk.CTkLabel, ctk.CTkLabel]] = {}
        for row_idx, pid in enumerate(_FREEZE_FRAME_PIDS):
            name = _PID_NAMES.get(pid, f"PID {pid:#04x}")
            bg = ("gray92", "gray17") if row_idx % 2 == 0 else ("gray88", "gray14")
            for col in range(3):
                ctk.CTkFrame(
                    ff_scroll, fg_color=bg, corner_radius=0, height=28
                ).grid(row=row_idx, column=col, sticky="nsew", padx=0, pady=0)
            name_lbl = ctk.CTkLabel(
                ff_scroll, text=f"0x{pid:02X}  {name}", anchor="w", fg_color=bg,
            )
            val_lbl  = ctk.CTkLabel(ff_scroll, text="—", anchor="w", fg_color=bg)
            unit_lbl = ctk.CTkLabel(ff_scroll, text="",  anchor="w", fg_color=bg)
            name_lbl.grid(row=row_idx, column=0, sticky="w", padx=10, pady=3)
            val_lbl.grid(row=row_idx,  column=1, sticky="w", padx=10, pady=3)
            unit_lbl.grid(row=row_idx, column=2, sticky="w", padx=10, pady=3)
            self._ff_rows[pid] = (name_lbl, val_lbl, unit_lbl)

    # ── Monitors tab ─────────────────────────────────────────────────────────

    # Column spec: (header_text, weight, minsize, anchor)
    _MON_COLS = [
        ("Description", 4, 200, "w"),
        ("Actual",       2,  90, "w"),
        ("Min",          2,  90, "w"),
        ("Max",          2,  90, "w"),
        ("Status",       2, 110, "e"),
    ]

    def _build_monitors_tab(self) -> None:
        tab = self._tabs.tab("Monitors")

        toolbar = ctk.CTkFrame(tab, fg_color="transparent")
        toolbar.pack(fill="x", padx=10, pady=(10, 4))

        self._read_mon_btn = ctk.CTkButton(
            toolbar, text="Read Monitors", command=self._read_monitors,
            width=160, state="disabled",
        )
        self._read_mon_btn.pack(side="left")

        self._mon_status_lbl = ctk.CTkLabel(toolbar, text="", anchor="w")
        self._mon_status_lbl.pack(side="left", padx=12)

        # Scrollable frame — header lives at row 0 inside so alignment is exact
        self._mon_frame = ctk.CTkScrollableFrame(tab)
        self._mon_frame.pack(fill="both", expand=True, padx=10, pady=(4, 6))
        for col, (_, weight, minsize, _) in enumerate(self._MON_COLS):
            self._mon_frame.columnconfigure(col, weight=weight, minsize=minsize)

        # Header row (row 0) — same grid as data rows, guaranteed alignment
        hdr_bg = ("gray85", "gray20")
        for col in range(len(self._MON_COLS)):
            ctk.CTkFrame(
                self._mon_frame, fg_color=hdr_bg, corner_radius=0, height=30
            ).grid(row=0, column=col, sticky="nsew")
        for col, (text, _, _, anchor) in enumerate(self._MON_COLS):
            sticky = "e" if anchor == "e" else "w"
            ctk.CTkLabel(
                self._mon_frame, text=text,
                font=ctk.CTkFont(weight="bold"), anchor=anchor, fg_color=hdr_bg,
            ).grid(row=0, column=col, sticky=sticky, padx=10, pady=4)

        # Data widgets tracked so they can be cleared on re-read
        self._mon_data_widgets: list = []

    # ── Vehicle Info tab ─────────────────────────────────────────────────────

    def _build_vehicle_info_tab(self) -> None:
        tab = self._tabs.tab("Vehicle Info")

        self._read_vinfo_btn = ctk.CTkButton(
            tab, text="Read Vehicle Info", command=self._read_vehicle_info,
            width=180, state="disabled",
        )
        self._read_vinfo_btn.pack(anchor="w", padx=10, pady=(10, 4))

        scroll = ctk.CTkScrollableFrame(tab)
        scroll.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        scroll.columnconfigure(0, weight=0, minsize=220)
        scroll.columnconfigure(1, weight=1)

        row_idx = 0

        # Mode 09 section ─────────────────────────────────────────────────────
        ctk.CTkLabel(
            scroll, text="OBD-II Vehicle Info  (Mode 09)",
            font=ctk.CTkFont(weight="bold"), anchor="w",
        ).grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=12, pady=(8, 4))
        row_idx += 1

        self._vinfo_labels: dict[str, ctk.CTkLabel] = {}
        for key, label in [
            ("vin", "VIN"),
            ("cal_id", "Calibration ID"),
            ("cvn", "CVN"),
            ("ecu_name", "ECU Name"),
        ]:
            ctk.CTkLabel(
                scroll, text=f"{label}:", font=ctk.CTkFont(weight="bold"),
                anchor="e", width=200,
            ).grid(row=row_idx, column=0, sticky="e", padx=12, pady=6)
            val_lbl = ctk.CTkLabel(scroll, text="—", anchor="w")
            val_lbl.grid(row=row_idx, column=1, sticky="w", padx=12, pady=6)
            self._vinfo_labels[key] = val_lbl
            row_idx += 1

        # UDS DIDs section ────────────────────────────────────────────────────
        sep = ctk.CTkFrame(scroll, height=2, fg_color=("gray70", "gray35"))
        sep.grid(row=row_idx, column=0, columnspan=2, sticky="ew", padx=12, pady=(10, 4))
        row_idx += 1

        ctk.CTkLabel(
            scroll, text="UDS DIDs  (Mode 22)",
            font=ctk.CTkFont(weight="bold"), anchor="w",
        ).grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 4))
        row_idx += 1

        self._ecu_labels: dict[int, ctk.CTkLabel] = {}
        for did, label in ECU_DIDS:
            ctk.CTkLabel(
                scroll,
                text=f"0x{did:04X}  {label}:",
                font=ctk.CTkFont(weight="bold"),
                anchor="e",
                width=200,
            ).grid(row=row_idx, column=0, sticky="e", padx=12, pady=6)
            val_lbl = ctk.CTkLabel(scroll, text="—", anchor="w")
            val_lbl.grid(row=row_idx, column=1, sticky="w", padx=12, pady=6)
            self._ecu_labels[did] = val_lbl
            row_idx += 1

    # ── Transport builder ─────────────────────────────────────────────────────

    def _build_transport_from_ui(self) -> BaseTransport:
        t = self._transport_var.get()

        if t == "virtual":
            from obd2_mcp.transport.virtual import VirtualTransport
            return VirtualTransport()

        if t == "elm327":
            from obd2_mcp.transport.elm327 import ELM327Transport
            port = self._port_entry.get().strip() or None
            return ELM327Transport(port=port)

        if t == "j2534":
            from obd2_mcp.transport.j2534 import J2534Transport
            key = self._dll_var.get()
            custom = self._dll_custom_entry.get().strip()
            dll_path = custom if key == "custom…" and custom else J2534_KNOWN_DEVICES.get(key)
            return J2534Transport(dll_path=dll_path, can_fd=self._can_fd_var.get())

        if t == "kline":
            from obd2_mcp.transport.kline import KLineTransport
            key = self._kline_dll_var.get()
            dll_path = J2534_KNOWN_DEVICES.get(key)
            return KLineTransport(dll_path=dll_path, protocol=self._kline_proto_var.get())

        if t == "doip":
            from obd2_mcp.transport.doip import DoIPTransport
            host = self._doip_host_entry.get().strip() or settings.doip_host
            try:
                port = int(self._doip_port_entry.get().strip() or 13400)
            except ValueError:
                port = 13400
            try:
                src = int(self._doip_src_entry.get().strip() or "0E00", 16)
            except ValueError:
                src = settings.doip_source_addr
            try:
                tgt = int(self._doip_tgt_entry.get().strip() or "0010", 16)
            except ValueError:
                tgt = settings.doip_target_addr
            return DoIPTransport(host=host, port=port, source_addr=src, target_addr=tgt)

        raise ValueError(f"Unknown transport: {t}")

    # ── Connection logic ──────────────────────────────────────────────────────

    def _connect(self) -> None:
        transport_type = self._transport_var.get()
        log_event("CONNECT_ATTEMPT", transport=transport_type)
        self._set_status("● Connecting…")
        self._connect_btn.configure(state="disabled")
        try:
            transport = self._build_transport_from_ui()
        except Exception as exc:
            log_error("CONNECT_BUILD_FAILED", exc, transport=transport_type)
            self._set_status(f"● Error: {exc}", _CLR_STATUS_OFF)
            self._connect_btn.configure(state="normal")
            return

        instrument_transport(transport)
        self._transport = transport
        self._obd2 = OBD2Protocol(transport)
        self._uds = UDSProtocol(transport)
        self._kwp2000 = KWP2000Protocol(transport)
        self._wait_for(
            self._runner.submit(transport.connect()),
            self._on_connected, self._on_connect_err,
        )

    def _on_connected(self, _: Any) -> None:
        assert self._transport
        log_event("CONNECTED", transport=self._transport_var.get(),
                  device_info=self._transport.device_info)
        self._set_status(
            f"● Connected  —  {self._transport.device_info.get('name', self._transport_var.get())}"
        )
        self._connect_btn.configure(state="disabled")
        self._disconnect_btn.configure(state="normal")
        for btn in (
            self._poll_btn, self._read_dtc_btn, self._clear_dtc_btn,
            self._read_ff_btn, self._read_mon_btn, self._read_vinfo_btn,
        ):
            btn.configure(state="normal")

        info = self._transport.device_info
        self._device_info_box.configure(state="normal")
        self._device_info_box.delete("1.0", "end")
        self._device_info_box.insert(
            "end",
            "\n".join(f"{k}: {v}" for k, v in info.items()) if info else "Connected",
        )
        self._device_info_box.configure(state="disabled")

        # Kick off Mode 01 PID discovery in background
        self._discover_lbl.configure(text="Discovering PIDs…")
        self._wait_for(
            self._runner.submit(self._obd2.discover_supported_pids()),
            self._on_pids_discovered,
            lambda exc: self._discover_lbl.configure(text=""),
        )

    def _on_pids_discovered(self, pids: list[int]) -> None:
        # No PIDs means 0100 went unanswered → the adapter is connected but no
        # vehicle responded (key off, not plugged in, or bus fault).
        if not pids:
            self._discover_lbl.configure(text="No vehicle response — check key / connection")
            return
        named = [(p, _PID_NAMES.get(p, f"PID {p:#04x}")) for p in pids]
        self._active_pids = named
        log_event("PIDS_DISCOVERED", count=len(pids),
                  pids=[f"0x{p:02X}" for p in pids])
        if not self._polling:
            self._build_live_data_rows(named)
        count = len(pids)
        self._discover_lbl.configure(text=f"{count} PIDs discovered")
        # Vehicle present (0100 answered) → status bar goes green.
        if self._transport:
            name = self._transport.device_info.get("name", self._transport_var.get())
            self._set_status(f"● Connected  —  {name}  —  vehicle detected", _CLR_STATUS_OK)
        # Vehicle is confirmed awake — auto-detect the VIN (lightweight Mode 09).
        if self._obd2:
            self._wait_for(
                self._runner.submit(self._obd2.read_vin()),
                self._on_vin_detected, lambda exc: None,
            )

    def _on_vin_detected(self, vin: str | None) -> None:
        if not vin:
            return
        log_event("VIN_DETECTED", vin=vin)
        lbl = self._vinfo_labels.get("vin")
        if lbl:
            lbl.configure(text=vin)
        if self._transport:
            name = self._transport.device_info.get("name", self._transport_var.get())
            self._set_status(f"● Connected  —  {name}  —  VIN {vin}", _CLR_STATUS_OK)

    def _on_connect_err(self, exc: Exception) -> None:
        log_error("CONNECT_FAILED", exc, transport=self._transport_var.get())
        self._set_status(f"● Connection failed: {exc}", _CLR_STATUS_OFF)
        self._connect_btn.configure(state="normal")

    def _disconnect(self) -> None:
        log_event("DISCONNECT")
        if self._polling:
            self._stop_polling()
        if self._transport and self._transport.is_connected:
            self._runner.submit(self._transport.disconnect())
        self._transport = self._obd2 = self._uds = self._kwp2000 = None
        self._active_pids = list(_DEFAULT_PIDS)
        self._build_live_data_rows(self._active_pids)
        self._discover_lbl.configure(text="")
        self._set_status("● Disconnected", _CLR_STATUS_OFF)
        self._connect_btn.configure(state="normal")
        self._disconnect_btn.configure(state="disabled")
        for btn in (
            self._poll_btn, self._read_dtc_btn, self._clear_dtc_btn,
            self._read_ff_btn, self._read_mon_btn, self._read_vinfo_btn,
        ):
            btn.configure(state="disabled")

    # ── Live data polling ─────────────────────────────────────────────────────

    def _apply_interval(self) -> None:
        """Commit the typed refresh interval. Bound to Enter and the ✓ button so
        the value never changes mid-typing."""
        try:
            ms = max(100, int(float(self._interval_var.get()) * 1000))
        except ValueError:
            # Reject garbage — restore the entry to the last committed value.
            self._interval_var.set(f"{self._applied_interval_ms / 1000:g}")
            return
        self._applied_interval_ms = ms
        # Normalize the displayed text to what was actually applied.
        self._interval_var.set(f"{ms / 1000:g}")

    def _on_auto_toggled(self) -> None:
        self._auto_rate = self._auto_var.get()
        if self._auto_rate:
            self._interval_entry.configure(state="disabled")
            # Gray the whole ✓ box (not just dim the glyph) so "disabled" reads
            # clearly while Auto drives the rate.
            self._interval_apply_btn.configure(
                state="disabled", fg_color=("gray70", "gray30"),
            )
            self._smoothed_poll_ms = 500.0
        else:
            self._interval_entry.configure(state="normal")
            self._interval_apply_btn.configure(
                state="normal",
                fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
            )
            self._rate_lbl.configure(text="")

    def _toggle_polling(self) -> None:
        if self._polling:
            self._stop_polling()
        else:
            self._start_polling()

    def _start_polling(self) -> None:
        log_event("POLLING_START", pids=[f"0x{p:02X}" for p, _ in self._active_pids])
        self._polling = True
        self._poll_btn.configure(
            text="■  Stop Polling",
            fg_color=_BTN_RED[0],
            hover_color=_BTN_RED[1],
        )
        self._schedule_poll()

    def _stop_polling(self) -> None:
        log_event("POLLING_STOP")
        self._polling = False
        self._poll_btn.configure(
            text="▶  Start Polling",
            fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
            hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"],
        )
        if self._poll_job:
            self.after_cancel(self._poll_job)
            self._poll_job = None
        self._rate_lbl.configure(text="")

    def _schedule_poll(self) -> None:
        if not self._polling or not self._obd2:
            return
        import time as _time
        obd = self._obd2
        # Skip PIDs that have been culled after repeated timeouts — they are
        # advertised by the support bitmap but the ECU never answers them.
        pids = [pid for pid, _ in self._active_pids if pid not in self._pid_culled]
        self._last_polled_pids = pids
        self._poll_start_time = _time.monotonic()
        poll_timeout = settings.obd_poll_timeout

        async def _poll_all() -> list:
            return await asyncio.gather(
                *[obd.query_pid(p, timeout=poll_timeout) for p in pids],
                return_exceptions=True,
            )

        if self._auto_rate:
            wait_poll_ms = max(10, min(40, int(self._smoothed_poll_ms * 0.25)))
        else:
            wait_poll_ms = max(10, min(40, self._applied_interval_ms // 4))

        future = self._runner.submit(_poll_all())
        self._wait_for(future, self._on_poll_result, self._on_poll_error, poll_ms=wait_poll_ms)

    def _on_poll_error(self, exc: Exception) -> None:
        log_error("POLL_ERROR", exc)
        self._set_status(f"● Poll error: {exc}")
        if self._polling:
            ms = max(100, int(self._smoothed_poll_ms * 1.2)) if self._auto_rate else 2000
            self._poll_job = self.after(ms, self._schedule_poll)

    def _on_poll_result(self, results: list) -> None:
        import time as _time
        elapsed_ms = (_time.monotonic() - self._poll_start_time) * 1000
        self._smoothed_poll_ms = 0.7 * self._smoothed_poll_ms + 0.3 * elapsed_ms

        cull_after = settings.obd_poll_cull_after
        for pid, result in zip(getattr(self, "_last_polled_pids", []), results):
            row = self._pid_rows.get(pid)
            if row is None:
                continue
            _, val_lbl, unit_lbl = row

            # Determine whether this poll yielded a usable value.
            value: tuple[str, str] | None = None  # (text, unit)
            failed = False
            if isinstance(result, Exception) or result is None:
                failed = True
            elif isinstance(result, dict):
                if result.get("value") is not None:
                    value = (str(result["value"]), str(result.get("unit") or ""))
                elif "raw_hex" in result:
                    value = (result["raw_hex"], "hex")
                else:
                    failed = True
            else:
                value = (str(result), "")

            if value is not None:
                # Good read: clear failure streak, remember it, display it.
                self._pid_fail_counts[pid] = 0
                self._pid_last_value[pid] = value
                val_lbl.configure(text=value[0], text_color=_CLR_VALUE)
                unit_lbl.configure(text=value[1])
            elif failed:
                self._pid_fail_counts[pid] = self._pid_fail_counts.get(pid, 0) + 1
                # Hold the last good value (dimmed) instead of flickering to N/A.
                last = self._pid_last_value.get(pid)
                if last is not None:
                    val_lbl.configure(text=last[0], text_color=_CLR_MUTED)
                    unit_lbl.configure(text=last[1])
                else:
                    val_lbl.configure(text="N/A", text_color=_CLR_MUTED)
                    unit_lbl.configure(text="")
                # Cull PIDs the ECU never answers so they stop taxing every cycle.
                if cull_after and self._pid_fail_counts[pid] >= cull_after:
                    self._pid_culled.add(pid)
                    val_lbl.configure(text="—", text_color=_CLR_MUTED)
                    unit_lbl.configure(text="n/a")

        if self._polling:
            if self._auto_rate:
                ms = max(100, int(self._smoothed_poll_ms * 1.2))
                hz = 1000.0 / (self._smoothed_poll_ms + ms)
                self._rate_lbl.configure(text=f"~{hz:.1f} Hz  ({ms}ms)")
            else:
                ms = self._applied_interval_ms
            self._poll_job = self.after(ms, self._schedule_poll)

    # ── DTCs ──────────────────────────────────────────────────────────────────

    def _read_dtcs(self) -> None:
        if not self._obd2:
            return
        log_event("READ_DTCS")
        self._read_dtc_btn.configure(state="disabled", text="Reading…")
        self._dtc_count_lbl.configure(text="")
        self._stored_dtc_codes = []
        self._wait_for(
            self._runner.submit(self._obd2.read_dtcs()),
            self._on_stored_dtcs_ready, self._on_dtc_error,
        )

    def _on_stored_dtcs_ready(self, codes: list[str]) -> None:
        self._stored_dtc_codes = codes
        self._wait_for(
            self._runner.submit(self._obd2.read_pending_dtcs()),
            self._on_pending_dtcs_ready, self._on_dtc_error,
        )

    def _on_pending_dtcs_ready(self, pending: list[str]) -> None:
        self._read_dtc_btn.configure(state="normal", text="Read DTCs")
        active = self._stored_dtc_codes
        log_event("DTCS_READ", active=active, pending=pending)

        for w in self._dtc_data_widgets:
            w.destroy()
        self._dtc_data_widgets.clear()

        rows: list[tuple[str, str, str, bool]] = []  # (code, desc, status, is_active)
        seen: set[str] = set()
        for code in active:
            rows.append((code, _DTC_DESC.get(code, "—"), "Active", True))
            seen.add(code)
        for code in pending:
            if code not in seen:
                rows.append((code, _DTC_DESC.get(code, "—"), "Pending", False))

        if not rows:
            lbl = ctk.CTkLabel(
                self._dtc_scroll, text="No trouble codes found  ✓", anchor="w"
            )
            lbl.grid(row=1, column=0, columnspan=len(self._DTC_COLS),
                     sticky="w", padx=10, pady=8)
            self._dtc_data_widgets.append(lbl)
            self._dtc_count_lbl.configure(text="No DTCs  ✓")
            return

        active_count  = sum(1 for r in rows if r[3])
        pend_count    = len(rows) - active_count
        parts = []
        if active_count:
            parts.append(f"{active_count} Active")
        if pend_count:
            parts.append(f"{pend_count} Pending")
        self._dtc_count_lbl.configure(text="  ·  ".join(parts))

        for row_idx, (code, desc, status, is_active) in enumerate(rows):
            row = row_idx + 1
            bg = ("gray92", "gray17") if row_idx % 2 == 0 else ("gray88", "gray14")
            status_color = ("orange3", "orange") if is_active else None

            for col in range(len(self._DTC_COLS)):
                bg_frame = ctk.CTkFrame(
                    self._dtc_scroll, fg_color=bg, corner_radius=0, height=28
                )
                bg_frame.grid(row=row, column=col, sticky="nsew")
                self._dtc_data_widgets.append(bg_frame)

            cell_values = [code, desc, status]
            for col, (value, (_, _, _, anchor)) in enumerate(
                zip(cell_values, self._DTC_COLS)
            ):
                kw: dict = dict(text=value, anchor=anchor, fg_color=bg)
                if col == 0:
                    kw["font"] = ctk.CTkFont(family="Courier New", size=13)
                if col == 2 and status_color:
                    kw["text_color"] = status_color
                    kw["font"] = ctk.CTkFont(weight="bold")
                sticky = "e" if anchor == "e" else "w"
                lbl = ctk.CTkLabel(self._dtc_scroll, **kw)
                lbl.grid(row=row, column=col, sticky=sticky, padx=10, pady=3)
                self._dtc_data_widgets.append(lbl)

    def _on_dtc_error(self, exc: Exception) -> None:
        log_error("DTC_ERROR", exc)
        self._read_dtc_btn.configure(state="normal", text="Read DTCs")
        self._dtc_count_lbl.configure(text=f"Error: {exc}")

    def _confirm(self, title: str, message: str) -> bool:
        """Modal Yes/No confirmation. Returns True only if the user picks Yes.

        Used for destructive actions. "Yes" is styled in danger red since every
        caller is something that changes vehicle state.
        """
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.resizable(False, False)
        win.transient(self)
        result = {"ok": False}

        ctk.CTkLabel(
            win, text=message, wraplength=340, justify="left",
        ).pack(padx=20, pady=(22, 16), fill="x")

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(pady=(0, 18))

        def _choose(ok: bool) -> None:
            result["ok"] = ok
            win.destroy()

        ctk.CTkButton(
            btn_row, text="No", width=110, command=lambda: _choose(False),
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            btn_row, text="Yes", width=110,
            fg_color=_BTN_RED[0], hover_color=_BTN_RED[1],
            command=lambda: _choose(True),
        ).pack(side="left", padx=8)

        win.protocol("WM_DELETE_WINDOW", lambda: _choose(False))
        win.update_idletasks()
        # Center over the main window.
        x = self.winfo_rootx() + (self.winfo_width()  - win.winfo_width())  // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.after(50, win.grab_set)  # grab once the window is viewable
        win.wait_window()
        return result["ok"]

    def _clear_dtcs(self) -> None:
        if not self._obd2:
            return
        log_event("CLEAR_DTCS_REQUESTED")
        if not self._confirm(
            "Clear DTCs",
            "Clear all stored DTCs and reset the MIL (check-engine light)?",
        ):
            log_event("CLEAR_DTCS_CANCELLED")
            return
        log_event("CLEAR_DTCS_CONFIRMED")
        self._clear_dtc_btn.configure(state="disabled", text="Clearing…")
        self._wait_for(
            self._runner.submit(self._obd2.clear_dtcs()),
            self._on_dtcs_cleared, self._on_dtc_error,
        )

    def _on_dtcs_cleared(self, success: bool) -> None:
        log_event("DTCS_CLEARED", success=success)
        self._clear_dtc_btn.configure(state="normal", text="Clear DTCs")
        for w in self._dtc_data_widgets:
            w.destroy()
        self._dtc_data_widgets.clear()
        self._dtc_count_lbl.configure(
            text="DTCs cleared  ✓" if success else "Clear returned failure"
        )

    # ── Freeze Frame ──────────────────────────────────────────────────────────

    def _read_freeze_frame(self) -> None:
        if not self._obd2:
            return
        try:
            frame = int(self._ff_frame_var.get())
        except ValueError:
            frame = 0
        log_event("READ_FREEZE_FRAME", frame=frame)
        self._read_ff_btn.configure(state="disabled", text="Reading…")
        self._ff_status_lbl.configure(text="")
        self._wait_for(
            self._runner.submit(self._obd2.read_freeze_frame(frame=frame)),
            self._on_ff_read, self._on_ff_error,
        )

    def _on_ff_read(self, results: list[dict]) -> None:
        self._read_ff_btn.configure(state="normal", text="Read Freeze Frame")
        log_event("FREEZE_FRAME_READ", count=len(results), data=results)
        for result in results:
            pid_hex = result.get("pid", "0x00")
            try:
                pid = int(pid_hex, 16)
            except ValueError:
                continue
            row = self._ff_rows.get(pid)
            if row is None:
                continue
            _, val_lbl, unit_lbl = row
            if "error" in result:
                val_lbl.configure(text="N/A")
                unit_lbl.configure(text="")
            elif "value" in result:
                val_lbl.configure(text=str(result["value"]))
                unit_lbl.configure(text=str(result.get("unit") or ""))
            else:
                val_lbl.configure(text=result.get("raw_hex", "—"))
                unit_lbl.configure(text="hex")
        self._ff_status_lbl.configure(text="Freeze frame loaded")

    def _on_ff_error(self, exc: Exception) -> None:
        log_error("FREEZE_FRAME_ERROR", exc)
        self._read_ff_btn.configure(state="normal", text="Read Freeze Frame")
        self._ff_status_lbl.configure(text=f"Error: {exc}")

    # ── Monitors ─────────────────────────────────────────────────────────────

    def _read_monitors(self) -> None:
        if not self._obd2:
            return
        log_event("READ_MONITORS")
        self._read_mon_btn.configure(state="disabled", text="Reading…")
        self._mon_status_lbl.configure(text="")
        self._wait_for(
            self._runner.submit(self._obd2.read_monitor_tests()),
            self._on_monitors_read, self._on_monitors_error,
        )

    def _on_monitors_read(self, tests: list[dict]) -> None:
        self._read_mon_btn.configure(state="normal", text="Read Monitors")

        for w in self._mon_data_widgets:
            w.destroy()
        self._mon_data_widgets.clear()

        if not tests:
            lbl = ctk.CTkLabel(
                self._mon_frame, text="No monitor data returned.", anchor="w"
            )
            lbl.grid(row=1, column=0, columnspan=len(self._MON_COLS),
                     sticky="w", padx=10, pady=8)
            self._mon_data_widgets.append(lbl)
            self._mon_status_lbl.configure(text="")
            return

        for row_idx, test in enumerate(tests):
            row = row_idx + 1  # row 0 is the header
            bg = ("gray92", "gray17") if row_idx % 2 == 0 else ("gray88", "gray14")
            passed = test.get("passed", True)
            status_text = "PASS" if passed else "FAIL"
            status_color = ("gray15", "gray85") if passed else (_BTN_RED[0], "orange")

            # Background fill for each cell (same pattern as Live Data tab)
            for col in range(len(self._MON_COLS)):
                bg_frame = ctk.CTkFrame(
                    self._mon_frame, fg_color=bg, corner_radius=0, height=28
                )
                bg_frame.grid(row=row, column=col, sticky="nsew")
                self._mon_data_widgets.append(bg_frame)

            unit = test.get("unit", "")
            unit_suffix = f" {unit}" if unit else ""
            cell_values = [
                test.get("description", "—"),
                f"{test.get('actual', '—')}{unit_suffix}",
                f"{test.get('min',    '—')}{unit_suffix}",
                f"{test.get('max',    '—')}{unit_suffix}",
                status_text,
            ]
            for col, (value, (_, _, _, anchor)) in enumerate(
                zip(cell_values, self._MON_COLS)
            ):
                kwargs: dict = dict(text=str(value), anchor=anchor, fg_color=bg)
                if col == len(self._MON_COLS) - 1:  # Status column
                    lbl = ctk.CTkLabel(
                        self._mon_frame,
                        font=ctk.CTkFont(weight="bold"),
                        text_color=status_color,
                        **kwargs,
                    )
                else:
                    lbl = ctk.CTkLabel(self._mon_frame, **kwargs)
                lbl.grid(
                    row=row, column=col, sticky="ew",
                    padx=10, pady=3,
                )
                self._mon_data_widgets.append(lbl)

        pass_count = sum(1 for t in tests if t.get("passed", True))
        fail_count = len(tests) - pass_count
        summary = f"{pass_count} PASS"
        if fail_count:
            summary += f"  ·  {fail_count} FAIL"
        log_event("MONITORS_READ", total=len(tests), passed=pass_count,
                  failed=fail_count, tests=tests)
        self._mon_status_lbl.configure(text=summary)

    def _on_monitors_error(self, exc: Exception) -> None:
        log_error("MONITORS_ERROR", exc)
        self._read_mon_btn.configure(state="normal", text="Read Monitors")
        self._mon_status_lbl.configure(text=f"Error: {exc}")

    # ── Vehicle Info ──────────────────────────────────────────────────────────

    def _read_vehicle_info(self) -> None:
        if not self._obd2 or not self._uds:
            return
        log_event("READ_VEHICLE_INFO")
        self._read_vinfo_btn.configure(state="disabled", text="Reading…")
        obd = self._obd2
        uds = self._uds

        async def _read_all():
            m09 = await obd.read_vehicle_info()
            dids = await asyncio.gather(
                *[uds.read_data_by_id(did) for did, _ in ECU_DIDS],
                return_exceptions=True,
            )
            return m09, list(dids)

        self._wait_for(
            self._runner.submit(_read_all()),
            self._on_vinfo_read, self._on_vinfo_error,
        )

    def _on_vinfo_read(self, result: tuple) -> None:
        self._read_vinfo_btn.configure(state="normal", text="Read Vehicle Info")
        m09, did_results = result
        did_summary = {
            f"0x{did:04X}": (
                str(res.get("value") or res.get("hex") or res.get("raw", "—"))
                if isinstance(res, dict) else f"error: {res}"
            )
            for (did, _), res in zip(ECU_DIDS, did_results)
        }
        log_event("VEHICLE_INFO_READ", mode09=m09, dids=did_summary)

        for key in ("vin", "cal_id", "cvn", "ecu_name"):
            lbl = self._vinfo_labels.get(key)
            if lbl:
                lbl.configure(text=str(m09.get(key, "—")))

        for (did, _), res in zip(ECU_DIDS, did_results):
            lbl = self._ecu_labels.get(did)
            if lbl is None:
                continue
            if isinstance(res, Exception):
                lbl.configure(text=f"Error: {res}")
            elif isinstance(res, dict):
                val = res.get("value") or res.get("hex") or res.get("raw", "—")
                lbl.configure(text=str(val))
            else:
                lbl.configure(text=str(res) if res else "—")

    def _on_vinfo_error(self, exc: Exception) -> None:
        log_error("VEHICLE_INFO_ERROR", exc)
        self._read_vinfo_btn.configure(state="normal", text="Read Vehicle Info")
        self._set_status(f"● Vehicle info error: {exc}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, color: Any = _CLR_VALUE) -> None:
        self._status_var.set(msg)
        self._status_lbl.configure(text_color=color)

    def _wait_for(
        self,
        future: "asyncio.Future[Any]",
        on_ok,
        on_err,
        poll_ms: int = 40,
    ) -> None:
        """Poll a concurrent.futures.Future; fire the right callback on the UI thread."""
        def _check() -> None:
            if not future.done():
                self.after(poll_ms, _check)
                return
            exc = future.exception()
            if exc:
                on_err(exc)
            else:
                on_ok(future.result())

        self.after(poll_ms, _check)

    def _on_close(self) -> None:
        log_event("WINDOW_CLOSE")
        if self._polling:
            self._stop_polling()
        if self._transport and self._transport.is_connected:
            self._runner.submit(self._transport.disconnect())
        self._runner.stop()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    acquire_instance_lock()
    set_source("GUI ")
    log_system_info()
    install_crash_handler()
    log_event("SESSION_START")
    app = OBD2App()
    app.mainloop()
    log_event("SESSION_END")


if __name__ == "__main__":
    main()

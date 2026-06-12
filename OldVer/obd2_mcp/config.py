"""Configuration management for obd2-ai-tool."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TransportType(str, Enum):
    """Supported hardware transport backends."""

    ELM327 = "elm327"         # Serial/USB ELM327 adapter (python-obd)
    J2534 = "j2534"           # J2534 passthru DLL (Windows: Opus, Autel, etc.)
    SOCKETCAN = "socketcan"   # Linux SocketCAN
    KLINE = "kline"           # K-Line ISO 9141-2 / KWP2000 via J2534 (Honda, etc.)
    DOIP = "doip"             # DoIP ISO 13400-2 over Ethernet
    VIRTUAL = "virtual"       # Simulation / offline testing


# ---------------------------------------------------------------------------
# Known J2534 device registry
# DLL paths are the default install locations; override via env or config file
# ---------------------------------------------------------------------------
J2534_KNOWN_DEVICES: dict[str, str] = {
    # Opus IVS Supergoose Plus (J2534-04.04, 32-bit DLL — requires 32-bit Python)
    "opus_supergoose": r"C:\Program Files (x86)\OpusIVS\J2534\SuperGoose-Plus\SuperGoosePlus_0404_32.dll",
    # Opus IVS Supergoose Plus (J2534-05.00 extended, 32-bit)
    "opus_supergoose_0500": r"C:\Program Files (x86)\OpusIVS\J2534\SuperGoose-Plus\0500\SuperGoosePlus_0500_32.dll",
    # Autel Maxiflash VCMI (64-bit, SysWOW64 = 32-bit system folder — use 32-bit Python)
    "autel_vcmi":     r"C:\Windows\SysWOW64\VCMI432.DLL",
    "autel_vcmi2":    r"C:\Windows\SysWOW64\VCMI2432.dll",
    # OBDLink SX / MX (fallback ELM327-class via J2534)
    "obdlink":        r"C:\Program Files\OBDLink\J2534.dll",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OBD2_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Transport
    transport: TransportType = TransportType.J2534

    # J2534 settings (used when transport == j2534)
    j2534_dll: Optional[str] = Field(
        default=None,
        description=(
            "Path to J2534 passthru DLL. "
            "Use 'auto' to detect from registry. "
            "Or a key from J2534_KNOWN_DEVICES: opus_supergoose | autel_vcmi"
        ),
    )
    j2534_device_index: int = Field(
        default=0,
        description="Device index when multiple J2534 devices are present.",
    )

    # ELM327 settings (used when transport == elm327)
    elm327_port: Optional[str] = Field(
        default=None,
        description="Serial port for ELM327 (e.g. COM3, /dev/ttyUSB0). None = auto-detect.",
    )
    elm327_baudrate: int = Field(default=38400)
    elm327_fast: bool = Field(default=True)
    elm327_timeout: float = Field(default=10.0)

    # SocketCAN settings (used when transport == socketcan)
    socketcan_channel: str = Field(default="can0")
    socketcan_bitrate: int = Field(default=500_000)

    # CAN FD settings (used when transport == j2534 or socketcan with FD hardware)
    can_fd: bool = Field(default=False, description="Enable CAN FD mode")
    can_fd_data_rate: int = Field(default=2_000_000, description="CAN FD data phase bitrate (bps)")

    # Protocol settings
    # CAN arbitration IDs for UDS (override per-vehicle as needed)
    uds_tx_id: int = Field(default=0x7DF, description="UDS functional request ID")
    uds_rx_id: int = Field(default=0x7E8, description="UDS ECU response ID")
    uds_padding_byte: int = Field(default=0xCC)

    # Live-data polling
    obd_poll_timeout: float = Field(
        default=0.5,
        description=(
            "Per-PID timeout (seconds) for live-data polling. OBD-II responses on "
            "CAN arrive in tens of ms; a short value keeps the grid responsive when "
            "some advertised PIDs never answer. Distinct from the 5.0s send_raw default."
        ),
    )
    obd_read_timeout: float = Field(
        default=2.0,
        description=(
            "Per-request timeout (seconds) for on-demand batch reads — freeze "
            "frame, Mode 06 monitors, Mode 09 vehicle info. A real CAN response "
            "arrives in tens of ms, so 2.0s is ample headroom while keeping a "
            "no-data/unsupported read from hanging the full 5.0s send_raw default "
            "for every PID in the batch."
        ),
    )
    obd_poll_cull_after: int = Field(
        default=3,
        description=(
            "Drop a PID from the live-data rotation after this many consecutive "
            "timeouts/errors. 0 disables culling."
        ),
    )

    # K-Line settings (used when transport == kline)
    kline_protocol: str = Field(
        default="iso14230",
        description="K-Line protocol: 'iso14230' (KWP2000) or 'iso9141'",
    )
    kline_target_addr: int = Field(default=0x33, description="K-Line target address (0x33 = OBD-II functional)")
    kline_source_addr: int = Field(default=0xF1, description="K-Line tester source address")

    # DoIP settings (used when transport == doip)
    doip_host: str = Field(default="169.254.0.10", description="DoIP gateway IP address")
    doip_port: int = Field(default=13400, description="DoIP TCP port (default 13400)")
    doip_source_addr: int = Field(default=0x0E00, description="Tester logical address")
    doip_target_addr: int = Field(default=0x0010, description="ECU logical address")

    # MCP server settings
    mcp_server_name: str = "obd2-ai-tool"
    mcp_log_level: str = "INFO"

    # Anthropic API key (for direct API calls, separate from MCP)
    anthropic_api_key: Optional[str] = Field(default=None)

    def resolve_j2534_dll(self) -> Optional[str]:
        """Resolve j2534_dll to an actual file path."""
        if self.j2534_dll is None:
            return None
        if self.j2534_dll in J2534_KNOWN_DEVICES:
            return J2534_KNOWN_DEVICES[self.j2534_dll]
        return self.j2534_dll


settings = Settings()

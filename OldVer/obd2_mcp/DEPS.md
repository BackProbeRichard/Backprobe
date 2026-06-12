# Dependencies

_Last updated: 2026-06-06 (v0.1.2)_

Update this file whenever a package is added, removed, or has its version constraint changed in `pyproject.toml`.

---

## Core dependencies

| Package | Version | Role |
|---|---|---|
| `python-obd` | `>=0.7.3` | OBD-II Mode 01–09 PID layer (ELM327 comms) |
| `udsoncan` | `>=1.25.2` | UDS ISO-14229 protocol — bidirectional tests, DID reads |
| `python-can` | `>=4.4.2` | CAN bus hardware abstraction — J2534, SocketCAN backends |
| `can-isotp` | `>=2.0.7` | User-space ISO-TP framing for multi-frame CAN messages |
| `cantools` | `>=39.0.0` | DBC/KCD signal database parsing (SavvyCAN-compatible) |
| `mcp[cli]` | `>=1.0.0` | Anthropic MCP SDK — exposes diagnostic tools to Claude |
| `anthropic` | `>=0.40.0` | Anthropic API client |
| `customtkinter` | `>=5.2.0` | GUI framework (dark mode, tkinter-based) |
| `pydantic` | `>=2.0.0` | Config validation and management |
| `pydantic-settings` | `>=2.0.0` | Settings management via env vars and .env files |
| `anyio` | `>=4.0.0` | Async runtime support |
| `rich` | `>=13.0.0` | Terminal logging output |
| `typer` | `>=0.12.0` | CLI entry points |

## Dev dependencies

| Package | Version | Role |
|---|---|---|
| `pytest` | `>=8.0.0` | Test runner |
| `pytest-asyncio` | `>=0.23.0` | Async test support |
| `pytest-mock` | `>=3.14.0` | Mocking |
| `ruff` | `>=0.4.0` | Linter / formatter |
| `mypy` | `>=1.10.0` | Static type checking |

## Hardware notes

- J2534 DLLs are 32-bit stdcall — Python must be **32-bit** (3.12-32)
- Tested adapters: Opus IVS Supergoose Plus (`OpusJ2534.dll`), Autel Maxiflash VCMI (`J2534.dll`)

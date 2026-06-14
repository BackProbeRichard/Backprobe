"""Shared test isolation.

Anything that logs through session_log (the daemon, the virtual backend) or
tokenizes a VIN (the event log) must not touch the real keyring at
~/.config/backprobe or scatter logs into the repo. This autouse fixture points
session_log at a per-test temp dir with a fixed key, so tokens are deterministic
and the real keyring is never read or written during tests.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_session_log(tmp_path, monkeypatch):
    from backprobe import session_log as sl

    monkeypatch.setenv("BACKPROBE_VIN_KEY", "00" * 32)        # deterministic token key
    cfg = tmp_path / "bp_cfg"
    monkeypatch.setattr(sl, "_CONFIG_DIR", cfg, raising=False)
    monkeypatch.setattr(sl, "_KEYRING_PATH", cfg / "vin_keyring.tsv", raising=False)
    monkeypatch.setattr(sl, "_key_cache", None, raising=False)
    monkeypatch.setattr(sl, "_known_tokens", None, raising=False)
    monkeypatch.setattr(sl, "_log_file", None, raising=False)
    sl.init(tmp_path / "bp_logs")

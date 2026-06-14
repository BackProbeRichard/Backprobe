"""VIN log-tokenization tests. Synthetic VINs only — no real vehicle data.

Guards write-time tokenization in session_log: every VIN becomes a stable
keyed token (plaintext + ASCII-in-hex), decodable via the local keyring, with
the real VIN never landing in a log. A fixed key + temp config dir keep these
deterministic and isolated from any real keyring.
"""

import importlib

import pytest

VIN = "1G1TEST00000ABCDE"   # synthetic, valid VIN charset (no I/O/Q)
VIN2 = "1G1TEST00000ZZZZZ"


@pytest.fixture()
def sl(tmp_path, monkeypatch):
    """session_log with a deterministic key and an isolated keyring."""
    monkeypatch.setenv("BACKPROBE_VIN_KEY", "00" * 32)
    monkeypatch.setenv("BACKPROBE_CONFIG", str(tmp_path))
    import backprobe.session_log as mod
    importlib.reload(mod)   # re-read env-based key/config paths
    return mod


def test_token_stable_and_distinct(sl):
    assert sl.vin_token(VIN) == sl.vin_token(VIN)      # same VIN -> same token
    assert sl.vin_token(VIN) != sl.vin_token(VIN2)     # different VIN -> different
    assert len(sl.vin_token(VIN)) == 16


def test_plaintext_vin_partially_tokenized(sl):
    out = sl.tokenize_vins(f"vin09='{VIN}'  vin22=''")
    assert VIN not in out                              # full VIN gone
    assert VIN[:11] in out                             # make/model/year kept
    assert VIN[11:] not in out                         # serial dropped
    assert sl.vin_token(VIN) in out                    # token appended


def test_hex_byte_dump_partially_tokenized(sl):
    vin_hex = VIN.encode().hex()
    line = f"CAN_EXCHANGE  sent=0902  recv=490201{vin_hex}  elapsed_ms=16.0"
    out = sl.tokenize_vins(line)
    assert vin_hex not in out                          # full VIN hex gone
    assert VIN[:11].encode().hex() in out              # 1-11 still present in hex
    assert sl.vin_token(VIN).encode().hex() in out     # token's hex present


def test_token_decodes_back_to_vin(sl):
    sl.tokenize_vins(f"vin='{VIN}'")                   # records the keyring
    assert sl.decode_token(sl.vin_token(VIN)) == VIN
    assert sl.decode_token("deadbeefdeadbeef") is None


def test_no_vin_is_noop(sl):
    text = "CAN_EXCHANGE  sent=0100  recv=4100be3f8003  elapsed_ms=12.0"
    assert sl.tokenize_vins(text) == text


def test_write_path_tokenizes_and_keeps_vin_out_of_file(sl, tmp_path):
    sl.init(tmp_path / "logs")
    log = sl.get_logger("TEST")
    log.event("VIN_READ", vin=VIN)
    contents = sl.session_log_path().read_text()
    assert VIN not in contents                         # full VIN never on disk
    assert VIN[11:] not in contents                    # serial never on disk
    assert sl.vin_token(VIN) in contents
    assert "PARTIAL but usable" in contents            # explanatory header present

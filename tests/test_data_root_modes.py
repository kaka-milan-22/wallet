"""data_root() must enforce 0o700 on its directory and migrate any
legacy 0o644 sensitive file to 0o600 on first call.

This is the defense-in-depth pass the user's existing install needs:
audit.log / policy.json end up world-readable on macOS because pyplatform
default umask is 0o022 and editors honour it. Tightening only on write
leaves read-only sessions exposed forever.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from wallet.core import config as config_mod


@pytest.fixture(autouse=True)
def _reset_tightened_cache():
    """The module memoizes which roots it's already tightened. Wipe between
    tests so each case sees a fresh tightening pass."""
    config_mod._TIGHTENED_ROOTS.clear()
    yield
    config_mod._TIGHTENED_ROOTS.clear()


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    return tmp_path


def test_fresh_data_root_is_created_0o700(isolated_root: Path):
    # Remove the dir so data_root has to mkdir it
    isolated_root.rmdir()
    p = config_mod.data_root()
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o700, f"expected 0o700 on fresh dir, got {oct(mode)}"


def test_loose_directory_mode_is_tightened(isolated_root: Path):
    """If `~/Library/Application Support/wallet/` already exists at 0o755
    (the macOS default), data_root() must tighten it to 0o700."""
    os.chmod(isolated_root, 0o755)
    config_mod.data_root()
    mode = stat.S_IMODE(isolated_root.stat().st_mode)
    assert mode == 0o700


def test_legacy_audit_log_0o644_is_migrated_on_data_root_call(isolated_root: Path):
    """A read-only session (e.g. `wallet info` / `wallet balance`) must
    still upgrade a legacy 0o644 audit.log without waiting for the next
    broadcast."""
    audit = isolated_root / "audit.log"
    audit.write_text('{"kind":"send"}\n')
    os.chmod(audit, 0o644)
    config_mod.data_root()
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600


def test_legacy_policy_json_0o644_is_migrated(isolated_root: Path):
    p = isolated_root / "policy.json"
    p.write_text('{"max_per_tx":{}}')
    os.chmod(p, 0o644)
    config_mod.data_root()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_chains_json_with_api_key_is_tightened(isolated_root: Path):
    """`chains.json` is user-edited (no wallet writer) and frequently
    embeds API keys in rpc_url. Editors leave it 0o644."""
    p = isolated_root / "chains.json"
    p.write_text('{"mainnet":{"rpc_url":"https://eth.example/v1/SECRET_KEY"}}')
    os.chmod(p, 0o644)
    config_mod.data_root()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_already_correct_modes_are_untouched(isolated_root: Path):
    """Idempotent: a call when everything's already 0o700/0o600 doesn't
    flap mtimes or modes."""
    audit = isolated_root / "audit.log"
    audit.write_text("{}\n")
    os.chmod(audit, 0o600)
    mtime_before = audit.stat().st_mtime_ns
    config_mod.data_root()
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600
    assert audit.stat().st_mtime_ns == mtime_before, "chmod fired unnecessarily"


def test_unreadable_file_is_skipped_silently(isolated_root: Path):
    """A path the wallet can't stat (broken symlink, permission denied)
    must not crash `data_root` — every command depends on it."""
    bad = isolated_root / "state.json"
    bad.symlink_to("/nonexistent/target/that/does/not/resolve")
    # Should not raise
    p = config_mod.data_root()
    assert p == isolated_root


def test_tightening_runs_once_per_root_per_process(isolated_root: Path):
    """Repeated `data_root()` calls during a single command shouldn't
    repeatedly stat every sensitive file. Verify via the memo set."""
    config_mod.data_root()
    config_mod.data_root()
    config_mod.data_root()
    assert isolated_root in config_mod._TIGHTENED_ROOTS
    assert len(config_mod._TIGHTENED_ROOTS) == 1


def test_swapping_wallet_home_re_tightens_new_root(tmp_path: Path, monkeypatch):
    """Tests + multi-tenant containers swap WALLET_HOME at runtime; the
    new dir must also get tightened, not just the first one seen."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir(mode=0o755)
    b.mkdir(mode=0o755)

    monkeypatch.setenv("WALLET_HOME", str(a))
    config_mod.data_root()
    assert stat.S_IMODE(a.stat().st_mode) == 0o700

    monkeypatch.setenv("WALLET_HOME", str(b))
    config_mod.data_root()
    assert stat.S_IMODE(b.stat().st_mode) == 0o700

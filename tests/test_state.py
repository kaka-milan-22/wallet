"""State file roundtrip + lookup helpers."""

import os
from pathlib import Path

import pytest

from wallet.storage import state as state_mod
from wallet.storage.state import (
    AccountEntry,
    TokenEntry,
    WalletState,
    WatchEntry,
    load_state,
    save_state,
)


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch):
    """Redirect state files into a per-test temp directory."""
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "state.json")
    return tmp_path


def test_load_state_returns_empty_when_missing(isolated_state):
    s = load_state()
    assert s.default_account is None
    assert s.default_chain == "sepolia"
    assert s.accounts == []
    assert s.book == {}
    assert s.watch == []
    assert s.tokens == []


def test_save_load_roundtrip(isolated_state):
    s = WalletState(
        default_account="main",
        accounts=[
            AccountEntry(
                name="main",
                address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
                derivation_path="m/44'/60'/0'/0/0",
                vault_key="wallet/main/mnemonic",
            )
        ],
        book={"vitalik": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"},
        watch=[WatchEntry(address="0xabcd" + "00" * 18, label="cold")],
        tokens=[
            TokenEntry(
                symbol="USDC",
                address="0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
                decimals=6,
                chain="sepolia",
            )
        ],
    )
    save_state(s)

    loaded = load_state()
    assert loaded == s


def test_state_file_is_user_only(isolated_state):
    save_state(WalletState())
    p = state_mod.state_path()
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600, f"state file should be 0600, got {oct(mode)}"


def test_find_account(isolated_state):
    s = WalletState(
        accounts=[
            AccountEntry(
                name="a",
                address="0x" + "11" * 20,
                derivation_path="m/44'/60'/0'/0/0",
                vault_key="wallet/a/mnemonic",
            ),
            AccountEntry(
                name="b",
                address="0x" + "22" * 20,
                derivation_path="m/44'/60'/0'/0/1",
                vault_key="wallet/a/mnemonic",
            ),
        ]
    )
    assert s.find_account("a").address == "0x" + "11" * 20
    assert s.find_account("b").address == "0x" + "22" * 20
    assert s.find_account("nope") is None


def test_get_default_account_falls_back_to_first(isolated_state):
    s = WalletState(
        accounts=[
            AccountEntry(
                name="first",
                address="0x" + "33" * 20,
                derivation_path="m/44'/60'/0'/0/0",
                vault_key="wallet/first/mnemonic",
            ),
        ]
    )
    # No default_account set — should return first
    assert s.get_default_account().name == "first"

    # With explicit default
    s.default_account = "first"
    assert s.get_default_account().name == "first"

    # Default points to non-existent account → None
    s.default_account = "ghost"
    assert s.get_default_account() is None


def test_invalid_state_json_raises(isolated_state, tmp_path):
    (tmp_path / "state.json").write_text("{not valid json")
    with pytest.raises(Exception):
        load_state()

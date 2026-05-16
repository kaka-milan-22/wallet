"""`wallet contract call` CLI smoke + arg-error paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wallet.cli.app import app


SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TARGET = "0x" + "44" * 20


def _write_state(tmp_path: Path) -> None:
    state = {
        "default_chain": "sepolia",
        "accounts": [{
            "name": "main",
            "address": SENDER,
            "derivation_path": "m/44'/60'/0'/0/0",
            "vault_key": "stub",
        }],
        "book": {}, "watch": [], "tokens": [],
    }
    (tmp_path / "state.json").write_text(json.dumps(state))


# --- help + import smoke ----------------------------------------------------


def test_contract_group_help_resolves():
    r = CliRunner().invoke(app, ["contract", "--help"])
    assert r.exit_code == 0
    assert "call" in r.output


def test_contract_call_help_resolves():
    r = CliRunner().invoke(app, ["contract", "call", "--help"])
    assert r.exit_code == 0
    assert "function" in r.output.lower() or "signature" in r.output.lower()


def test_contract_call_body_runs_with_all_names_resolved(monkeypatch, tmp_path: Path):
    """NameError guard — same pattern as tests/test_cli_lp.py.

    Stub the first external helper the body reaches (`make_web3_or_exit`);
    if any imported name is unbound the body NameErrors before the stub fires.
    """
    sentinel = SystemExit(99)

    def early_exit(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr("wallet.cli.contract.make_web3_or_exit", early_exit)
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)

    r = CliRunner().invoke(app, [
        "contract", "call", TARGET, "transfer(address,uint256)",
        "0x" + "11" * 20, "100",
        "--dry-run",
    ])
    assert not isinstance(r.exception, NameError), (
        f"contract call body unbound name: {r.exception}"
    )
    assert r.exit_code == 99


# --- CLI argument validation ------------------------------------------------


def test_contract_call_rejects_bad_target_address(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)
    # `make_web3_or_exit` is reached before target validation; stub it.
    monkeypatch.setattr(
        "wallet.cli.contract.make_web3_or_exit",
        lambda cfg, command: object(),
    )

    r = CliRunner().invoke(app, [
        "--json", "contract", "call", "0xshort",
        "transfer(address,uint256)", "0x" + "11" * 20, "100",
        "--dry-run",
    ])
    assert r.exit_code == 2
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["error"] == "validation_error"
    assert "20-byte address" in env["reason"]


def test_contract_call_rejects_bad_signature(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)

    # Stub w3 — sig parse runs inside prepare_contract_call after make_web3.
    class _W3Stub:
        class eth:
            chain_id = 11155111
    monkeypatch.setattr(
        "wallet.cli.contract.make_web3_or_exit",
        lambda cfg, command: _W3Stub(),
    )

    r = CliRunner().invoke(app, [
        "--json", "contract", "call", TARGET,
        "not_a_signature",  # missing parens
        "--dry-run",
    ])
    assert r.exit_code == 2
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["error"] == "validation_error"
    assert "name(types" in env["reason"]

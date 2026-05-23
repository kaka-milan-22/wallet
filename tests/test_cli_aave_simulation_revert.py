"""`wallet aave <write>` must surface simulation reverts as a JSON envelope.

`core.tx._simulate` wraps web3's `ContractLogicError` as
`RuntimeError("simulation reverted: …")`. The 5 aave write commands
(supply / withdraw / borrow / repay / faucet) previously only caught
`ContractLogicError`, letting RuntimeError escape as a raw Python
traceback. Observed on Sepolia 2026-05-23 when `aave withdraw --max`
hit Aave's `LIQUIDITY_LESS_THAN_AVAILABLE` (a transient pool-drained
state) — the traceback dumped to stderr and broke any agent parsing
the JSON envelope.

Each command must now also catch RuntimeError and emit the same
`simulation_reverted` envelope as the ContractLogicError branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wallet.cli.app import app
from wallet.protocols.aave import AaveReserve


SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def _write_state(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(json.dumps({
        "default_account": "alice",
        "default_chain": "sepolia",
        "accounts": [{
            "name": "alice",
            "address": SENDER,
            "derivation_path": "m/44'/60'/0'/0/0",
            "vault_key": "stub",
        }],
        "book": {}, "watch": [], "tokens": [],
    }))


def _write_permissive_policy(tmp_path: Path) -> None:
    (tmp_path / "policy.json").write_text(json.dumps({
        "max_per_tx": {},
        "max_per_day": {},
        "recipient_allowlist": [],
        "contract_allowlist": ["0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951"],
        "first_send_warn": False,
        "deny_unlimited_approve": True,
    }))


def _setup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)
    _write_permissive_policy(tmp_path)
    monkeypatch.setattr("wallet.cli.aave.make_web3_or_exit", lambda cfg, command: None)
    monkeypatch.setattr(
        "wallet.cli.aave.resolve_aave_reserve",
        lambda w3, chain, q: AaveReserve(
            symbol="LINK",
            asset_address="0xf8Fb3713D459D7C1018BD0A49D19b4C44290EBE5",
            decimals=18,
        ),
    )


def _raise_runtime(*args, **kwargs):
    raise RuntimeError("simulation reverted: ('execution reverted', '0x')")


@pytest.mark.parametrize("cmd, patch_target, extra_args", [
    ("supply",   "wallet.cli.aave.prepare_supply",   ["LINK", "10"]),
    ("withdraw", "wallet.cli.aave.prepare_withdraw", ["LINK", "10"]),
    ("borrow",   "wallet.cli.aave.prepare_borrow",   ["LINK", "1"]),
    ("repay",    "wallet.cli.aave.prepare_repay",    ["LINK", "1"]),
    ("faucet",   "wallet.cli.aave.prepare_faucet_mint", ["LINK", "100"]),
])
def test_aave_write_runtime_error_surfaces_as_json_envelope(
    monkeypatch, tmp_path: Path, cmd: str, patch_target: str, extra_args: list[str]
):
    """Each `wallet aave <write>` must catch RuntimeError from the simulate
    layer and emit a clean `simulation_reverted` envelope. Before the fix
    these escaped as Python tracebacks."""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(patch_target, _raise_runtime)

    r = CliRunner().invoke(app, ["--json", "aave", cmd, *extra_args])

    # No traceback in output (would contain `Traceback` if escaped).
    assert "Traceback" not in r.output, (
        f"`aave {cmd}` leaked a Python traceback instead of emitting a "
        f"clean envelope. Output:\n{r.output}"
    )
    assert r.exit_code == 3, r.output
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["ok"] is False
    assert env["error"] == "simulation_reverted"
    assert env["command"] == f"aave.{cmd}"
    assert "simulation reverted" in env["reason"]

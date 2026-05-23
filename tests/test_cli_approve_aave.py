"""`wallet approve set` auto-resolves token symbol via Aave V3 reserve list
when the spender is the configured aave_v3.pool — the gotcha documented in
docs/TESTING.md "Two different USDCs" where `USDC` resolves to Circle's
Sepolia USDC but Aave's pool wants its own mock token at a different
address. Without this auto-resolve the approve lands on the wrong token
and the subsequent `aave supply` reverts with insufficient_allowance.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from typer.testing import CliRunner

from wallet.cli.app import app
from wallet.protocols.aave import AaveReserve


SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
AAVE_POOL_SEPOLIA = "0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951"
AAVE_MOCK_USDC = "0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8"
CIRCLE_USDC = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
NON_AAVE_SPENDER = "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E"  # Uniswap router


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
    """Policy that allows arbitrary spenders/contracts so dry-run reaches
    prepare_erc20_approve. We only care about which token address goes in."""
    (tmp_path / "policy.json").write_text(json.dumps({
        "max_per_tx": {},
        "max_per_day": {},
        "recipient_allowlist": [],
        "contract_allowlist": [AAVE_POOL_SEPOLIA, NON_AAVE_SPENDER],
        "first_send_warn": False,
        "deny_unlimited_approve": True,
    }))


def _capture_prepare(monkeypatch):
    """Patch `prepare_erc20_approve` to record what token address it was
    asked to approve, then raise SystemExit(77) so we don't need a full
    signer / broadcast harness."""
    captured: dict = {}

    def fake_prepare(w3, chain, sender, token_addr, spender, amount, symbol, decimals):
        captured["token_address"] = token_addr
        captured["spender"] = spender
        captured["symbol"] = symbol
        captured["decimals"] = decimals
        raise SystemExit(77)

    monkeypatch.setattr("wallet.cli.approve.prepare_erc20_approve", fake_prepare)
    return captured


def test_approve_set_to_aave_pool_auto_resolves_to_aave_mock(monkeypatch, tmp_path: Path):
    """USDC → Aave mock USDC, not Circle USDC, when spender == aave pool."""
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)
    _write_permissive_policy(tmp_path)

    monkeypatch.setattr("wallet.cli.approve.make_web3_or_exit", lambda cfg, command: MagicMock())
    monkeypatch.setattr(
        "wallet.cli.approve.resolve_aave_reserve",
        lambda w3, chain, q: AaveReserve(
            symbol="USDC", asset_address=AAVE_MOCK_USDC, decimals=6,
        ),
    )

    # If `resolve_token` is called, the auto-resolve branch failed.
    called_via_regular_resolve = {"hit": False}

    def fake_resolve_token(*a, **kw):
        called_via_regular_resolve["hit"] = True
        raise AssertionError("regular resolve_token should not be reached when spender is aave pool")

    monkeypatch.setattr("wallet.cli.approve.resolve_token", fake_resolve_token)

    captured = _capture_prepare(monkeypatch)

    r = CliRunner().invoke(app, [
        "approve", "set", "USDC", AAVE_POOL_SEPOLIA, "10",
        "--dry-run",
    ])
    assert r.exit_code == 77, r.output
    assert called_via_regular_resolve["hit"] is False
    assert captured["token_address"] == AAVE_MOCK_USDC
    assert captured["symbol"] == "USDC"
    assert captured["decimals"] == 6


def test_approve_set_to_non_aave_spender_uses_regular_resolve(monkeypatch, tmp_path: Path):
    """Non-Aave spender (e.g. Uniswap router) must still resolve via the
    regular token resolver — the auto-mock logic must NOT hijack approvals
    to other DEXes/contracts."""
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)
    _write_permissive_policy(tmp_path)

    monkeypatch.setattr("wallet.cli.approve.make_web3_or_exit", lambda cfg, command: MagicMock())

    aave_was_called = {"hit": False}

    def fake_aave_resolve(*a, **kw):
        aave_was_called["hit"] = True
        raise AssertionError("aave reserve resolution must not run for non-aave spenders")

    monkeypatch.setattr("wallet.cli.approve.resolve_aave_reserve", fake_aave_resolve)

    from wallet.core.tokens import TokenInfo
    monkeypatch.setattr(
        "wallet.cli.approve.resolve_token",
        lambda w3, cfg, state, q: TokenInfo(
            symbol="USDC", address=CIRCLE_USDC, decimals=6,
        ),
    )

    captured = _capture_prepare(monkeypatch)

    r = CliRunner().invoke(app, [
        "approve", "set", "USDC", NON_AAVE_SPENDER, "10",
        "--dry-run",
    ])
    assert r.exit_code == 77, r.output
    assert aave_was_called["hit"] is False
    assert captured["token_address"] == CIRCLE_USDC


def test_approve_set_to_aave_pool_with_raw_address_skips_auto_resolve(monkeypatch, tmp_path: Path):
    """If the user passes a raw 0x token address (not a symbol) we must not
    second-guess them — they took manual control of which token to approve.
    Even when spender is the aave pool, the address gets through unchanged."""
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)
    _write_permissive_policy(tmp_path)

    monkeypatch.setattr("wallet.cli.approve.make_web3_or_exit", lambda cfg, command: MagicMock())

    aave_was_called = {"hit": False}

    def fake_aave_resolve(*a, **kw):
        aave_was_called["hit"] = True
        raise AssertionError("aave reserve resolution must not run when user passes raw 0x address")

    monkeypatch.setattr("wallet.cli.approve.resolve_aave_reserve", fake_aave_resolve)

    from wallet.core.tokens import TokenInfo
    custom_addr = "0xDEADbeefDEADbeefDEADbeefDEADbeefDEADbeef"
    monkeypatch.setattr(
        "wallet.cli.approve.resolve_token",
        lambda w3, cfg, state, q: TokenInfo(
            symbol="???", address=custom_addr, decimals=18,
        ),
    )

    captured = _capture_prepare(monkeypatch)

    r = CliRunner().invoke(app, [
        "approve", "set", custom_addr, AAVE_POOL_SEPOLIA, "10",
        "--dry-run",
    ])
    assert r.exit_code == 77, r.output
    assert aave_was_called["hit"] is False
    assert captured["token_address"] == custom_addr

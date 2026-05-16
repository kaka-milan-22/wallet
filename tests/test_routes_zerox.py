"""ZeroExRoute — request shape, response parsing, error mapping."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo
from wallet.protocols.routes.base import NoRouteError
from wallet.protocols.routes.zerox import (
    ZEROX_NATIVE_SENTINEL,
    ZEROX_QUOTE_URL,
    ZeroExRoute,
)


# 0x v2 AllowanceHolder is deterministically deployed to the same address on
# every supported chain; the test fixture uses an arbitrary constant standing
# in for that pinned address. Every well-formed test payload routes both
# `transaction.to` and `issues.allowance.spender` to this address — the pin
# behaviour we ship in 1.05 (security_review.md F7) rejects any quote that
# returns something else.
HOLDER = "0x" + "33" * 20

CHAIN = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="http://invalid",
    explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}",
    native_symbol="ETH",
    builtin_tokens={"WETH": "0x" + "ee" * 20},
    protocols={"zerox": {"allowance_holder": HOLDER}},
)

USDC = TokenInfo(symbol="USDC", address="0x" + "11" * 20, decimals=6)
WETH = TokenInfo(symbol="WETH", address="0x" + "ee" * 20, decimals=18)
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def _ok_response(payload: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


def _zerox_quote_payload(
    *,
    buy_amount: int,
    min_buy: int | None = None,
    spender: str | None = None,
    to: str = HOLDER,
) -> dict:
    return {
        "buyAmount": str(buy_amount),
        "minBuyAmount": str(min_buy if min_buy is not None else buy_amount),
        "transaction": {
            "to": to,
            "data": "0xdeadbeef",
            "value": "0",
        },
        "issues": {
            "allowance": (
                {"actual": "0", "spender": spender}
                if spender is not None
                else None
            ),
        },
        "route": {
            "fills": [{"source": "Uniswap_V3", "proportionBps": "10000"}],
        },
    }


def test_no_api_key_raises_no_route(monkeypatch):
    monkeypatch.delenv("WALLET_ZEROX_API_KEY", raising=False)
    with pytest.raises(NoRouteError, match="WALLET_ZEROX_API_KEY"):
        ZeroExRoute().quote(
            w3=MagicMock(), chain=CHAIN, sender=SENDER,
            token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
        )


def test_successful_quote_returns_parsed_fields(monkeypatch):
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    payload = _zerox_quote_payload(
        buy_amount=2 * 10**18, min_buy=199 * 10**16,
        spender=HOLDER,  # pin requires spender == chain's AllowanceHolder
    )

    captured: dict = {}
    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _ok_response(payload)

    monkeypatch.setattr(httpx, "get", fake_get)

    q = ZeroExRoute().quote(
        w3=MagicMock(), chain=CHAIN, sender=SENDER,
        token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
    )

    assert captured["url"] == ZEROX_QUOTE_URL
    assert captured["headers"]["0x-api-key"] == "test-key"
    assert captured["headers"]["0x-version"] == "v2"
    assert captured["params"]["chainId"] == 11155111
    assert captured["params"]["sellToken"] == USDC.address
    assert captured["params"]["buyToken"] == WETH.address
    assert captured["params"]["sellAmount"] == "1000000"
    assert captured["params"]["slippageBps"] == 50

    from web3 import Web3
    assert q.route_provider == "0x"
    assert "Uniswap_V3" in q.route_description
    assert q.amount_out_expected_wei == 2 * 10**18
    assert q.amount_out_min_wei == 199 * 10**16
    assert q.spender == Web3.to_checksum_address(HOLDER)
    assert q.data == "0xdeadbeef"


def test_native_eth_uses_sentinel(monkeypatch):
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    payload = _zerox_quote_payload(buy_amount=10**6)
    payload["transaction"]["value"] = str(10**16)  # native input goes into msg.value

    captured: dict = {}
    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return _ok_response(payload)
    monkeypatch.setattr(httpx, "get", fake_get)

    eth = TokenInfo(symbol="ETH", address=WETH.address, decimals=18, is_native=True)
    q = ZeroExRoute().quote(
        w3=MagicMock(), chain=CHAIN, sender=SENDER,
        token_in=eth, token_out=USDC, amount_in_wei=10**16, slippage_bps=50,
    )

    assert captured["params"]["sellToken"].lower() == ZEROX_NATIVE_SENTINEL.lower()
    assert q.value == 10**16


def test_fake_native_symbol_does_not_use_sentinel(monkeypatch):
    """Regression for security_review.md Vuln 1.

    A token with symbol="ETH" but is_native=False is an ERC-20 — 0x routing
    must send its actual address as sellToken, NOT the native sentinel.
    """
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    payload = _zerox_quote_payload(buy_amount=10**6)

    captured: dict = {}
    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return _ok_response(payload)
    monkeypatch.setattr(httpx, "get", fake_get)

    fake_eth = TokenInfo(
        symbol="ETH", address="0x" + "ba" * 20, decimals=18,  # is_native=False
    )
    ZeroExRoute().quote(
        w3=MagicMock(), chain=CHAIN, sender=SENDER,
        token_in=fake_eth, token_out=USDC, amount_in_wei=10**16, slippage_bps=50,
    )

    assert captured["params"]["sellToken"].lower() != ZEROX_NATIVE_SENTINEL.lower()
    assert captured["params"]["sellToken"].lower() == fake_eth.address.lower()


def test_404_response_raises_no_route(monkeypatch):
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    r = MagicMock()
    r.status_code = 404
    r.text = "no liquidity"
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: r)

    with pytest.raises(NoRouteError, match="no route"):
        ZeroExRoute().quote(
            w3=MagicMock(), chain=CHAIN, sender=SENDER,
            token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
        )


def test_500_response_raises_no_route(monkeypatch):
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    r = MagicMock()
    r.status_code = 500
    r.text = "internal error"
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: r)

    with pytest.raises(NoRouteError, match="HTTP 500"):
        ZeroExRoute().quote(
            w3=MagicMock(), chain=CHAIN, sender=SENDER,
            token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
        )


def test_network_error_raises_no_route(monkeypatch):
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")

    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(NoRouteError, match="ConnectError"):
        ZeroExRoute().quote(
            w3=MagicMock(), chain=CHAIN, sender=SENDER,
            token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
        )


def test_missing_transaction_field_raises(monkeypatch):
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    r = _ok_response({"buyAmount": "1", "minBuyAmount": "1"})  # no transaction
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: r)

    with pytest.raises(NoRouteError, match="missing `transaction`"):
        ZeroExRoute().quote(
            w3=MagicMock(), chain=CHAIN, sender=SENDER,
            token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
        )


def test_spender_falls_back_to_tx_to_when_no_allowance_issue(monkeypatch):
    """When 0x omits `issues.allowance` (e.g. native input, already approved),
    `spender` falls back to `transaction.to` — which under the 1.05 pin is
    also the AllowanceHolder, so the pin check still passes."""
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    payload = _zerox_quote_payload(buy_amount=10**6)  # spender=None, to=HOLDER
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _ok_response(payload))

    q = ZeroExRoute().quote(
        w3=MagicMock(), chain=CHAIN, sender=SENDER,
        token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
    )
    from web3 import Web3
    assert q.spender == Web3.to_checksum_address(HOLDER)


# --- 1.05: AllowanceHolder pin (security_review.md F7) ----------------------


def test_quote_rejected_when_spender_does_not_match_pinned_holder(monkeypatch):
    """Compromised api.0x.org returns `spender` = a router the user already
    approved for another protocol (e.g. stale UniswapV3Router approval). The
    pin must reject before this reaches policy / signing."""
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    rogue_spender = "0x" + "ba" * 20  # NOT the AllowanceHolder
    payload = _zerox_quote_payload(
        buy_amount=10**6, spender=rogue_spender, to=HOLDER,
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _ok_response(payload))

    with pytest.raises(NoRouteError, match="unexpected spender"):
        ZeroExRoute().quote(
            w3=MagicMock(), chain=CHAIN, sender=SENDER,
            token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
        )


def test_quote_rejected_when_tx_to_does_not_match_pinned_holder(monkeypatch):
    """Sibling attack: compromised quote routes tx.to to a router the user
    has a stale approval on; spender field happens to match the
    AllowanceHolder. The pin on `to` catches this."""
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    rogue_to = "0x" + "ba" * 20
    payload = _zerox_quote_payload(
        buy_amount=10**6, spender=HOLDER, to=rogue_to,
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _ok_response(payload))

    with pytest.raises(NoRouteError, match="routes tx to"):
        ZeroExRoute().quote(
            w3=MagicMock(), chain=CHAIN, sender=SENDER,
            token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
        )


def test_quote_rejected_when_chain_has_no_allowance_holder_configured(monkeypatch):
    """Fail-closed: any chain without an explicit AllowanceHolder entry must
    refuse 0x routing rather than silently regressing to the pre-1.05
    behaviour where any spender was accepted."""
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    unconfigured_chain = ChainConfig(
        name="some-unsupported-chain", chain_id=42,
        rpc_url="http://invalid", explorer_api_url="http://invalid",
        explorer_tx_url="http://invalid/{tx}", native_symbol="ETH",
        builtin_tokens={"WETH": "0x" + "ee" * 20},
        # no protocols.zerox.allowance_holder
    )
    payload = _zerox_quote_payload(buy_amount=10**6, spender=HOLDER, to=HOLDER)
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _ok_response(payload))

    with pytest.raises(NoRouteError, match="zerox.allowance_holder"):
        ZeroExRoute().quote(
            w3=MagicMock(), chain=unconfigured_chain, sender=SENDER,
            token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
        )


def test_pin_check_is_case_insensitive(monkeypatch):
    """0x returns lowercase hex; our pinned config has checksum casing. Match
    must succeed regardless of source casing."""
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    payload = _zerox_quote_payload(
        buy_amount=10**6, spender=HOLDER.lower(), to=HOLDER.upper().replace("0X", "0x"),
    )
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _ok_response(payload))

    # Should NOT raise
    q = ZeroExRoute().quote(
        w3=MagicMock(), chain=CHAIN, sender=SENDER,
        token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
    )
    from web3 import Web3
    assert q.spender == Web3.to_checksum_address(HOLDER)

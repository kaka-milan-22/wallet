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


CHAIN = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="http://invalid",
    explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}",
    native_symbol="ETH",
    builtin_tokens={"WETH": "0x" + "ee" * 20},
)

USDC = TokenInfo(symbol="USDC", address="0x" + "11" * 20, decimals=6)
WETH = TokenInfo(symbol="WETH", address="0x" + "ee" * 20, decimals=18)
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def _ok_response(payload: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    return r


def _zerox_quote_payload(*, buy_amount: int, min_buy: int | None = None, spender: str | None = None) -> dict:
    return {
        "buyAmount": str(buy_amount),
        "minBuyAmount": str(min_buy if min_buy is not None else buy_amount),
        "transaction": {
            "to": "0x" + "33" * 20,
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
        spender="0x" + "44" * 20,
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

    assert q.route_provider == "0x"
    assert "Uniswap_V3" in q.route_description
    assert q.amount_out_expected_wei == 2 * 10**18
    assert q.amount_out_min_wei == 199 * 10**16
    assert q.spender == "0x" + "44" * 20
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

    eth = TokenInfo(symbol="ETH", address=WETH.address, decimals=18)
    q = ZeroExRoute().quote(
        w3=MagicMock(), chain=CHAIN, sender=SENDER,
        token_in=eth, token_out=USDC, amount_in_wei=10**16, slippage_bps=50,
    )

    assert captured["params"]["sellToken"].lower() == ZEROX_NATIVE_SENTINEL.lower()
    assert q.value == 10**16


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
    monkeypatch.setenv("WALLET_ZEROX_API_KEY", "test-key")
    # No issues.allowance — e.g. native input or already-approved
    payload = _zerox_quote_payload(buy_amount=10**6)  # spender=None
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _ok_response(payload))

    q = ZeroExRoute().quote(
        w3=MagicMock(), chain=CHAIN, sender=SENDER,
        token_in=USDC, token_out=WETH, amount_in_wei=10**6, slippage_bps=50,
    )
    # Falls back to transaction.to as spender
    assert q.spender == "0x" + ("33" * 20).upper()[:40].lower().rjust(40, "0") or len(q.spender) == 42
    # Just confirm it's a valid checksum address derived from "0x" + "33"*20
    from web3 import Web3
    assert q.spender == Web3.to_checksum_address("0x" + "33" * 20)

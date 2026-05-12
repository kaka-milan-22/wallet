"""AutoFallbackRoute — provider iteration order, all-failure aggregation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo
from wallet.protocols.routes.auto import AutoFallbackRoute
from wallet.protocols.routes.base import NoRouteError, Quote, RouteProvider


CHAIN = ChainConfig(
    name="sepolia", chain_id=11155111,
    rpc_url="http://invalid", explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}", native_symbol="ETH",
)
TOK_A = TokenInfo(symbol="A", address="0x" + "11" * 20, decimals=18)
TOK_B = TokenInfo(symbol="B", address="0x" + "22" * 20, decimals=18)


def _make_quote(provider_name: str) -> Quote:
    return Quote(
        route_provider=provider_name,
        route_description=f"{provider_name}: A > B",
        to="0x" + "33" * 20,
        data="0x",
        value=0,
        token_in_address=TOK_A.address,
        token_out_address=TOK_B.address,
        token_in_symbol="A", token_out_symbol="B",
        token_in_decimals=18, token_out_decimals=18,
        amount_in_wei=10**18,
        amount_out_expected_wei=10**18,
        amount_out_min_wei=10**18,
        spender="0x" + "33" * 20,
    )


class _StubProvider(RouteProvider):
    def __init__(self, name: str, *, raise_error: str | None = None):
        self.name = name
        self._raise = raise_error
        self.call_count = 0

    def quote(self, w3, chain, sender, token_in, token_out, amount_in_wei, slippage_bps):
        self.call_count += 1
        if self._raise is not None:
            raise NoRouteError(self._raise)
        return _make_quote(self.name)


def test_first_provider_wins_when_succeeds():
    p1 = _StubProvider("first")
    p2 = _StubProvider("second")
    auto = AutoFallbackRoute([p1, p2])

    q = auto.quote(
        w3=MagicMock(), chain=CHAIN, sender="0x" + "aa" * 20,
        token_in=TOK_A, token_out=TOK_B,
        amount_in_wei=10**18, slippage_bps=50,
    )

    assert q.route_provider == "first"
    assert p1.call_count == 1
    assert p2.call_count == 0  # never reached


def test_fallback_to_second_when_first_no_route():
    p1 = _StubProvider("first", raise_error="no liquidity")
    p2 = _StubProvider("second")
    auto = AutoFallbackRoute([p1, p2])

    q = auto.quote(
        w3=MagicMock(), chain=CHAIN, sender="0x" + "aa" * 20,
        token_in=TOK_A, token_out=TOK_B,
        amount_in_wei=10**18, slippage_bps=50,
    )

    assert q.route_provider == "second"
    assert p1.call_count == 1
    assert p2.call_count == 1


def test_all_providers_failing_raises_aggregated_error():
    p1 = _StubProvider("first", raise_error="api key missing")
    p2 = _StubProvider("second", raise_error="no pools")
    auto = AutoFallbackRoute([p1, p2])

    with pytest.raises(NoRouteError) as exc:
        auto.quote(
            w3=MagicMock(), chain=CHAIN, sender="0x" + "aa" * 20,
            token_in=TOK_A, token_out=TOK_B,
            amount_in_wei=10**18, slippage_bps=50,
        )

    msg = str(exc.value)
    assert "all route providers failed" in msg
    assert "first: api key missing" in msg
    assert "second: no pools" in msg


def test_empty_providers_list_rejected_at_construction():
    with pytest.raises(ValueError, match="at least one provider"):
        AutoFallbackRoute([])

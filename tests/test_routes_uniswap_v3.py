"""UniswapV3DirectRoute — fee tier selection, calldata, slippage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo
from wallet.protocols.routes.base import NoRouteError
from wallet.protocols.routes.uniswap_v3 import (
    FEE_TIERS,
    UniswapV3DirectRoute,
    _apply_slippage,
)


SEPOLIA = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="http://invalid",
    explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}",
    native_symbol="ETH",
    builtin_tokens={
        "USDC": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
        "WETH": "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",
    },
    protocols={
        "uniswap_v3": {
            "swap_router_v2": "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E",
            "quoter_v2": "0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3",
            "factory": "0x0227628f3F023bb0B980b67D528571c95c6DaC1c",
        },
    },
)

USDC = TokenInfo(symbol="USDC", address=SEPOLIA.builtin_tokens["USDC"], decimals=6)
WETH = TokenInfo(symbol="WETH", address=SEPOLIA.builtin_tokens["WETH"], decimals=18)
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def test_apply_slippage_basic():
    assert _apply_slippage(10_000, 50) == 9_950  # 0.5% slip on 10000 → 9950
    assert _apply_slippage(10_000, 0) == 10_000
    assert _apply_slippage(10_000, 10_000) == 0


def test_apply_slippage_rejects_out_of_range():
    with pytest.raises(ValueError):
        _apply_slippage(100, -1)
    with pytest.raises(ValueError):
        _apply_slippage(100, 10_001)


def _make_quoter_call_handler(per_fee_outputs: dict[int, int | None]):
    """Build a callable that simulates the chained `.call()` on a Quoter contract.

    `per_fee_outputs[fee] = amount_out` for tiers with liquidity, or None to
    simulate a revert.
    """
    def call_handler(args_tuple):
        # args_tuple = (tokenIn, tokenOut, amountIn, fee, sqrtPriceLimitX96)
        fee = args_tuple[3]
        out = per_fee_outputs.get(fee)
        if out is None:
            raise Exception(f"pool reverts at fee={fee} (no liquidity)")
        # Quoter returns (amountOut, sqrtPriceX96After, ticksCrossed, gasEstimate)
        return [out, 0, 0, 100_000]
    return call_handler


def _make_w3_mock(per_fee_outputs: dict[int, int | None]):
    """Build a w3 mock whose `eth.contract().functions.quoteExactInputSingle().call()`
    routes through the per-fee handler, and whose `encode_abi` returns a stub hex."""
    w3 = MagicMock()
    call_handler = _make_quoter_call_handler(per_fee_outputs)

    def contract_factory(address, abi):
        c = MagicMock()
        # Quoter side
        def quote_fn(params):
            inner = MagicMock()
            inner.call = lambda: call_handler(params)
            return inner
        c.functions.quoteExactInputSingle = quote_fn
        # Router side: encode_abi returns deterministic stub
        c.encode_abi = lambda name, args: "0x" + name.encode().hex() + "00" * 32
        return c

    w3.eth.contract = contract_factory
    return w3


def test_quote_picks_fee_tier_with_max_output():
    # 500 bps tier has best output; 100 and 3000 also have liquidity but worse;
    # 10000 has no liquidity.
    outputs = {100: 99, 500: 105, 3000: 100, 10000: None}
    w3 = _make_w3_mock(outputs)

    route = UniswapV3DirectRoute()
    q = route.quote(
        w3=w3, chain=SEPOLIA, sender=SENDER,
        token_in=USDC, token_out=WETH,
        amount_in_wei=10_000_000,  # 10 USDC
        slippage_bps=50,
    )

    assert q.route_provider == "uniswap_v3"
    assert q.amount_out_expected_wei == 105
    assert q.amount_out_min_wei == _apply_slippage(105, 50)
    assert "500bps" in q.route_description
    assert q.token_in_symbol == "USDC"
    assert q.token_out_symbol == "WETH"
    assert q.spender == Web3.to_checksum_address(
        SEPOLIA.protocols["uniswap_v3"]["swap_router_v2"]
    )
    assert q.value == 0  # ERC-20 input, no msg.value
    assert q.data.startswith("0x")


def test_quote_no_liquidity_raises_no_route():
    # All tiers revert
    outputs = {tier: None for tier in FEE_TIERS}
    w3 = _make_w3_mock(outputs)

    route = UniswapV3DirectRoute()
    with pytest.raises(NoRouteError, match="no liquidity"):
        route.quote(
            w3=w3, chain=SEPOLIA, sender=SENDER,
            token_in=USDC, token_out=WETH,
            amount_in_wei=1, slippage_bps=50,
        )


def test_quote_native_eth_in_sets_value_and_uses_weth():
    outputs = {500: 100}
    w3 = _make_w3_mock(outputs)

    eth_token = TokenInfo(symbol="ETH", address=SEPOLIA.builtin_tokens["WETH"], decimals=18)
    route = UniswapV3DirectRoute()
    q = route.quote(
        w3=w3, chain=SEPOLIA, sender=SENDER,
        token_in=eth_token, token_out=USDC,
        amount_in_wei=10**16,  # 0.01 ETH
        slippage_bps=100,
    )

    # value passes amount_in_wei (router wraps internally)
    assert q.value == 10**16
    # token_in_address in quote uses WETH (calldata reference)
    assert q.token_in_address == Web3.to_checksum_address(SEPOLIA.builtin_tokens["WETH"])
    # symbol preserved on outside
    assert q.token_in_symbol == "ETH"

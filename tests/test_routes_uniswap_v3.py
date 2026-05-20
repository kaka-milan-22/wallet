"""UniswapV3DirectRoute — fee tier selection, calldata, slippage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo
from wallet.protocols.routes.base import NoRouteError
from wallet.core.slippage import apply_slippage_floor as _apply_slippage
from wallet.protocols.routes.uniswap_v3 import (
    FEE_TIERS,
    UniswapV3DirectRoute,
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
        # Router side: encode_abi returns deterministic stub. MagicMock
        # records every call so tests can inspect args (used by the
        # native-out unwrap test to verify exactInputSingle recipient and
        # the multicall composition).
        c.encode_abi = MagicMock(
            side_effect=lambda name, args: "0x" + name.encode().hex() + "00" * 32
        )
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

    eth_token = TokenInfo(
        symbol="ETH", address=SEPOLIA.builtin_tokens["WETH"], decimals=18,
        is_native=True,
    )
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


def test_quote_native_eth_out_wraps_swap_in_multicall_with_unwrap():
    """When token_out is native ETH the route must emit a multicall:
       1. exactInputSingle(recipient = ADDRESS_THIS sentinel 0x...0002)
       2. unwrapWETH9(amountMin, user)
    so the user receives real ETH instead of WETH.

    The test inspects encode_abi call args directly (rather than parsing
    the stubbed calldata bytes) so it verifies the composition without
    needing a real ABI encoder.
    """
    eth_out = TokenInfo(
        symbol="ETH", address=SEPOLIA.builtin_tokens["WETH"], decimals=18,
        is_native=True,
    )

    # Use a spy mock that records every encode_abi call so the test can
    # verify the multicall composition (which fn names were encoded and
    # with what args).
    seen_encode: list[tuple[str, list]] = []

    def spy_factory(address, abi):
        c = MagicMock()
        c.functions.quoteExactInputSingle = lambda params: MagicMock(
            call=lambda: [10**16, 0, 0, 100_000]
        )

        def encode(name, args):
            seen_encode.append((name, args))
            return "0x" + name.encode().hex() + "00" * 32

        c.encode_abi = encode
        return c

    spy_w3 = MagicMock()
    spy_w3.eth.contract = spy_factory
    route = UniswapV3DirectRoute()
    q = route.quote(
        w3=spy_w3, chain=SEPOLIA, sender=SENDER,
        token_in=USDC, token_out=eth_out,
        amount_in_wei=37_000_000, slippage_bps=100,
    )

    fn_names = [n for n, _ in seen_encode]
    assert "exactInputSingle" in fn_names, fn_names
    assert "unwrapWETH9" in fn_names, fn_names
    assert "multicall" in fn_names, fn_names

    # exactInputSingle.recipient must be ADDRESS_THIS sentinel (0x…0002),
    # not the user's EOA — that's what keeps WETH in the router for unwrap.
    swap_args = next(a for n, a in seen_encode if n == "exactInputSingle")[0]
    # swap_args is the tuple: (tokenIn, tokenOut, fee, recipient, amountIn, ...)
    recipient = swap_args[3]
    assert recipient.lower() == "0x0000000000000000000000000000000000000002", (
        f"swap recipient must be ADDRESS_THIS for unwrap to work, got {recipient}"
    )

    # unwrapWETH9.recipient must be the real user (so they actually get ETH).
    unwrap_args = next(a for n, a in seen_encode if n == "unwrapWETH9")
    assert unwrap_args[1].lower() == SENDER.lower(), (
        f"unwrap must send to user, got {unwrap_args[1]}"
    )

    # value=0 (ERC-20 input); token_out symbol preserved.
    assert q.value == 0
    assert q.token_out_symbol == "ETH"
    assert q.token_out_address == Web3.to_checksum_address(SEPOLIA.builtin_tokens["WETH"])


def test_quote_erc20_to_erc20_does_not_use_multicall():
    """When neither token is native, the calldata is a plain exactInputSingle —
    no multicall wrapper. Guards against regressing the unwrap path."""
    outputs = {500: 10**18}
    w3 = _make_w3_mock(outputs)

    route = UniswapV3DirectRoute()
    q = route.quote(
        w3=w3, chain=SEPOLIA, sender=SENDER,
        token_in=USDC, token_out=WETH,  # WETH as ERC-20, not is_native
        amount_in_wei=10**6,
        slippage_bps=100,
    )

    # Top-level call is exactInputSingle (not wrapped in multicall).
    assert "exactInputSingle".encode().hex() in q.data.lower()
    assert "multicall".encode().hex() not in q.data.lower(), (
        "ERC-20 → ERC-20 must not wrap in multicall"
    )


def test_quote_fake_native_symbol_does_not_set_value():
    """Regression for security_review.md Vuln 1.

    A token with symbol="ETH" but is_native=False is just an ERC-20 — it must
    NOT cause the route to send real native ETH via msg.value, and the calldata
    must reference the malicious token's actual address (so the on-chain swap
    fails cleanly) rather than WETH (which would silently sell real ETH).
    """
    outputs = {500: 100}
    w3 = _make_w3_mock(outputs)

    fake_eth = TokenInfo(
        symbol="ETH",
        address="0x" + "ba" * 20,  # attacker contract
        decimals=18,
        # is_native defaults to False
    )
    route = UniswapV3DirectRoute()
    q = route.quote(
        w3=w3, chain=SEPOLIA, sender=SENDER,
        token_in=fake_eth, token_out=USDC,
        amount_in_wei=10**16,
        slippage_bps=100,
    )

    assert q.value == 0, "fake-native ERC-20 must not consume native ETH"
    assert q.token_in_address == Web3.to_checksum_address(fake_eth.address)

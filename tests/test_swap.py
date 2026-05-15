"""prepare_swap orchestration: allowance pre-check, PreparedTx shape."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo
from wallet.protocols.routes.base import Quote, RouteProvider
from wallet.protocols.swap import InsufficientAllowance, prepare_swap


SEPOLIA = ChainConfig(
    name="sepolia", chain_id=11155111,
    rpc_url="http://invalid", explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}", native_symbol="ETH",
    builtin_tokens={"WETH": "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14"},
)
USDC = TokenInfo(symbol="USDC", address="0x" + "11" * 20, decimals=6)
WETH = TokenInfo(symbol="WETH", address=SEPOLIA.builtin_tokens["WETH"], decimals=18)
ROUTER = "0x" + "33" * 20
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


class FakeProvider(RouteProvider):
    name = "fake"

    def __init__(self, expected_out: int = 10**18):
        self._expected = expected_out

    def quote(self, w3, chain, sender, token_in, token_out, amount_in_wei, slippage_bps):
        is_native = token_in.symbol == chain.native_symbol
        return Quote(
            route_provider=self.name,
            route_description=f"{token_in.symbol} > 500bps > {token_out.symbol}",
            to=ROUTER,
            data="0xdeadbeef",
            value=amount_in_wei if is_native else 0,
            token_in_address=token_in.address,
            token_out_address=token_out.address,
            token_in_symbol=token_in.symbol,
            token_out_symbol=token_out.symbol,
            token_in_decimals=token_in.decimals,
            token_out_decimals=token_out.decimals,
            amount_in_wei=amount_in_wei,
            amount_out_expected_wei=self._expected,
            amount_out_min_wei=self._expected * (10_000 - slippage_bps) // 10_000,
            spender=ROUTER,
        )


def _w3_mock(allowance_value: int = 0):
    """w3 with eth.contract() returning a fake allowance and eth.estimate_gas/get_block/etc."""
    w3 = MagicMock()
    w3.eth.max_priority_fee = Web3.to_wei(2, "gwei")
    w3.eth.get_block.return_value = {"baseFeePerGas": Web3.to_wei(10, "gwei")}
    w3.eth.get_transaction_count.return_value = 5
    w3.eth.estimate_gas.return_value = 120_000
    w3.eth.call.return_value = b""

    # ERC-20 allowance lookup goes through eth.contract(...)
    def contract_factory(address, abi):
        c = MagicMock()
        c.functions.allowance.return_value.call.return_value = allowance_value
        return c

    w3.eth.contract = contract_factory
    return w3


def test_prepare_swap_erc20_with_allowance_builds_tx():
    w3 = _w3_mock(allowance_value=10**18)  # plenty
    provider = FakeProvider(expected_out=2 * 10**18)

    pt = prepare_swap(
        w3=w3, chain=SEPOLIA, sender=SENDER,
        token_in=USDC, token_out=WETH,
        amount_in_wei=10**6,  # 1 USDC
        slippage_bps=50,
        provider=provider,
    )

    desc = pt.description
    assert desc["kind"] == "swap"
    assert desc["from"] == Web3.to_checksum_address(SENDER)
    assert desc["to"] == ROUTER
    assert desc["amount_wei"] == 10**6
    assert desc["amount_unit"] == "USDC"
    assert desc["amount_decimals"] == 6
    assert desc["swap_token_in_address"] == USDC.address
    assert desc["swap_token_out_address"] == WETH.address
    assert desc["swap_token_out_symbol"] == "WETH"
    assert desc["swap_amount_out_expected_wei"] == 2 * 10**18
    assert desc["swap_amount_out_min_wei"] == 2 * 10**18 * 9_950 // 10_000
    assert desc["swap_slippage_bps"] == 50
    assert desc["swap_provider"] == "fake"
    assert "USDC > 500bps > WETH" == desc["swap_route"]
    assert pt.tx["to"] == ROUTER
    assert pt.tx["data"] == "0xdeadbeef"
    assert pt.tx["value"] == 0
    assert "nonce" not in pt.tx  # Tier 1.1: nonce is refreshed at sign-time
    assert pt.tx["chainId"] == 11155111
    assert pt.tx["type"] == 2
    assert pt.tx["gas"] == 120_000


def test_prepare_swap_insufficient_allowance_raises():
    w3 = _w3_mock(allowance_value=10**5)  # only 0.1 USDC approved
    provider = FakeProvider()

    with pytest.raises(InsufficientAllowance) as exc:
        prepare_swap(
            w3=w3, chain=SEPOLIA, sender=SENDER,
            token_in=USDC, token_out=WETH,
            amount_in_wei=10**6,  # want to swap 1 USDC
            slippage_bps=50,
            provider=provider,
        )

    e = exc.value
    assert e.token_symbol == "USDC"
    assert e.token_address == USDC.address
    assert e.spender == ROUTER
    assert e.current_wei == 10**5
    assert e.required_wei == 10**6


def test_prepare_swap_native_in_skips_allowance_check():
    # allowance_value=0 should be ignored when token_in is the native asset
    w3 = _w3_mock(allowance_value=0)
    provider = FakeProvider()

    eth_token = TokenInfo(
        symbol="ETH", address=SEPOLIA.builtin_tokens["WETH"], decimals=18,
        is_native=True,
    )
    pt = prepare_swap(
        w3=w3, chain=SEPOLIA, sender=SENDER,
        token_in=eth_token, token_out=USDC,
        amount_in_wei=10**16,
        slippage_bps=50,
        provider=provider,
    )

    assert pt.tx["value"] == 10**16
    assert pt.description["amount_unit"] == "ETH"


def test_prepare_swap_rejects_malicious_native_symbol():
    """Regression for security_review.md Vuln 1.

    A token whose contract returns symbol="ETH" (the chain's native symbol) but
    was NOT constructed via the CLI's native branch must still go through the
    allowance pre-check — it's an ERC-20, not real native ETH. Earlier code
    routed on `symbol == chain.native_symbol` and would have:
      - skipped the allowance check, and
      - let the route layer set value = amount_in_wei real native ETH.
    """
    w3 = _w3_mock(allowance_value=0)
    provider = FakeProvider()

    fake_native = TokenInfo(
        symbol="ETH",  # attacker-controlled symbol()
        address="0x" + "ba" * 20,  # arbitrary ERC-20 address — NOT WETH
        decimals=18,
        # is_native deliberately omitted (defaults to False)
    )

    with pytest.raises(InsufficientAllowance):
        prepare_swap(
            w3=w3, chain=SEPOLIA, sender=SENDER,
            token_in=fake_native, token_out=USDC,
            amount_in_wei=10**16,
            slippage_bps=50,
            provider=provider,
        )

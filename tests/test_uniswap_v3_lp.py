"""Tests for `protocols.uniswap_v3_lp` — read positions + prepare_* builders.

All Web3 interactions are MagicMock — no fork, no live RPC. The mocks return
hard-coded outputs for `positions()`, `slot0()`, `balanceOf`, `allowance`,
`estimate_gas`, `eth.call`, etc. so each path can be exercised
deterministically.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo, clear_token_info_cache
from wallet.core.uniswap_v3_math import MAX_UINT128
from wallet.protocols.swap import InsufficientAllowance
from wallet.protocols.uniswap_v3_lp import (
    NFPM_ABI,
    fetch_position,
    get_positions,
    prepare_collect,
    prepare_decrease_liquidity,
    prepare_increase_liquidity,
    prepare_mint,
)


# --- fixtures --------------------------------------------------------------


NFPM_ADDR = "0x1238536071E1c677A632429e3655c799b22cDA52"
FACTORY_ADDR = "0x0227628f3F023bb0B980b67D528571c95c6DaC1c"
USDC_ADDR = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
WETH_ADDR = "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14"
POOL_ADDR = "0x1234567890123456789012345678901234567890"
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


SEPOLIA = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="http://invalid",
    explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}",
    native_symbol="ETH",
    builtin_tokens={"USDC": USDC_ADDR, "WETH": WETH_ADDR},
    protocols={
        "uniswap_v3": {
            "swap_router_v2": "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E",
            "quoter_v2": "0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3",
            "factory": FACTORY_ADDR,
            "nonfungible_position_manager": NFPM_ADDR,
        },
    },
)


# Standard slot0 output: sqrtPriceX96 close to USDC/WETH at ~$3000 is well
# above 2**96; for tests with USDC as token0 and tick=0 we just use 2**96
# and a tick within the canonical range.
_DEFAULT_SLOT0 = [2**96, 0, 0, 0, 0, 0, True]

# Positions tuple matches the NFPM_ABI output ordering:
# (nonce, operator, token0, token1, fee, tickLower, tickUpper, liquidity,
#  fg0, fg1, owed0, owed1)
def _default_positions_tuple(
    *,
    token0=USDC_ADDR,
    token1=WETH_ADDR,
    fee=3000,
    tick_lower=-60,
    tick_upper=60,
    liquidity=10**18,
    owed0=0,
    owed1=0,
):
    return [
        0,                                                                 # nonce
        "0x0000000000000000000000000000000000000000",                      # operator
        Web3.to_checksum_address(token0),                                  # token0
        Web3.to_checksum_address(token1),                                  # token1
        fee, tick_lower, tick_upper, liquidity,
        0, 0,                                                              # fg0/fg1
        owed0, owed1,
    ]


class _W3Spec:
    """Mutable container the contract-factory closure reads from.

    Each test sets per-call return values here; the mocks pick them up
    via attribute lookup so multiple contract instances (NFPM, factory,
    pool, ERC-20 for allowance) all stay in sync.
    """

    def __init__(self) -> None:
        self.chain_id = SEPOLIA.chain_id
        self.positions_tuple: list[Any] | None = None
        self.slot0_tuple: list[Any] = list(_DEFAULT_SLOT0)
        self.pool_address: str = POOL_ADDR
        self.balance_of: dict[str, int] = {}
        self.token_owner_index: dict[tuple[str, int], int] = {}
        self.allowances: dict[tuple[str, str, str], int] = {}
        # token address (lower) → (symbol, decimals)
        self.token_meta: dict[str, tuple[str, int]] = {
            USDC_ADDR.lower(): ("USDC", 6),
            WETH_ADDR.lower(): ("WETH", 18),
        }
        # Set to a callable to raise inside estimate_gas / eth.call
        self.estimate_gas_raises: Exception | None = None
        self.simulate_raises: Exception | None = None
        self.estimate_gas_value: int = 250_000


def _make_w3_mock(spec: _W3Spec) -> Any:
    w3 = MagicMock()

    def contract_factory(address: str, abi: list[dict]):
        addr_l = address.lower()
        c = MagicMock()

        # Helper to wrap a value into the .call()-returning chain.
        def wrap_call(value):
            inner = MagicMock()
            inner.call = lambda: value
            return inner

        # --- NFPM (matched by address) ---
        if addr_l == NFPM_ADDR.lower():
            c.functions.balanceOf = lambda owner: wrap_call(
                spec.balance_of.get(owner.lower(), 0)
            )
            c.functions.tokenOfOwnerByIndex = lambda owner, idx: wrap_call(
                spec.token_owner_index[(owner.lower(), idx)]
            )
            c.functions.positions = lambda token_id: wrap_call(spec.positions_tuple)

            # Write builders: build_transaction returns a dict with gas set.
            def make_action(name):
                def wrapper(params_tuple):
                    inner = MagicMock()
                    def build_tx(base: dict):
                        return {
                            **base,
                            "to": Web3.to_checksum_address(NFPM_ADDR),
                            "data": "0x" + name.encode().hex(),
                            "value": 0,
                            "gas": spec.estimate_gas_value,
                            "nonce": 0,
                        }
                    inner.build_transaction = build_tx
                    return inner
                return wrapper

            c.functions.collect = make_action("collect")
            c.functions.decreaseLiquidity = make_action("decreaseLiquidity")
            c.functions.increaseLiquidity = make_action("increaseLiquidity")
            c.functions.mint = make_action("mint")

            # encode_abi: deterministic stub used by mint/increase multicall path
            c.encode_abi = lambda name, args: "0x" + name.encode().hex() + "00" * 32

        # --- Factory ---
        elif addr_l == FACTORY_ADDR.lower():
            c.functions.getPool = lambda *_args: wrap_call(spec.pool_address)

        # --- Pool ---
        elif addr_l == spec.pool_address.lower():
            c.functions.slot0 = lambda: wrap_call(spec.slot0_tuple)

        # --- ERC-20 (for allowance + symbol + decimals reads via fetch_token_info) ---
        else:
            meta = spec.token_meta.get(addr_l, ("???", 18))

            c.functions.symbol = lambda: wrap_call(meta[0])
            c.functions.decimals = lambda: wrap_call(meta[1])

            def alw_fn(owner, spender):
                key = (addr_l, owner.lower(), spender.lower())
                return wrap_call(spec.allowances.get(key, 0))
            c.functions.allowance = alw_fn

        return c

    w3.eth.contract = contract_factory
    w3.eth.chain_id = spec.chain_id
    w3.eth.max_priority_fee = 10**9  # 1 gwei
    w3.eth.get_block = lambda *_: {"baseFeePerGas": 2 * 10**9}

    def estimate_gas(_tx):
        if spec.estimate_gas_raises is not None:
            raise spec.estimate_gas_raises
        return spec.estimate_gas_value
    w3.eth.estimate_gas = estimate_gas

    def eth_call(_tx):
        if spec.simulate_raises is not None:
            raise spec.simulate_raises
        return b""
    w3.eth.call = eth_call

    return w3


@pytest.fixture(autouse=True)
def _wipe_token_cache():
    clear_token_info_cache()
    yield
    clear_token_info_cache()


# --- get_positions / fetch_position ----------------------------------------


def test_get_positions_returns_empty_when_owner_has_none():
    spec = _W3Spec()
    spec.balance_of[SENDER.lower()] = 0
    w3 = _make_w3_mock(spec)
    assert get_positions(w3, SEPOLIA, SENDER) == []


def test_fetch_position_resolves_pool_and_amounts():
    spec = _W3Spec()
    spec.positions_tuple = _default_positions_tuple()
    w3 = _make_w3_mock(spec)

    pos = fetch_position(w3, SEPOLIA, token_id=42)

    assert pos.token_id == 42
    assert pos.token0_address == Web3.to_checksum_address(USDC_ADDR)
    assert pos.token1_address == Web3.to_checksum_address(WETH_ADDR)
    assert pos.fee == 3000
    assert pos.tick_lower == -60
    assert pos.tick_upper == 60
    assert pos.pool_address == Web3.to_checksum_address(POOL_ADDR)
    assert pos.in_range is True  # current_tick=0 is in [-60, 60)
    # Both amounts > 0 inside range
    assert pos.amount0_wei > 0
    assert pos.amount1_wei > 0


def test_fetch_position_out_of_range_below():
    spec = _W3Spec()
    spec.positions_tuple = _default_positions_tuple(tick_lower=60, tick_upper=120)
    spec.slot0_tuple = [2**96, 0, 0, 0, 0, 0, True]  # current tick = 0, below range
    w3 = _make_w3_mock(spec)

    pos = fetch_position(w3, SEPOLIA, token_id=7)
    assert pos.in_range is False
    # All token0, no token1 when price below range
    assert pos.amount0_wei > 0
    assert pos.amount1_wei == 0


def test_get_positions_enumerates_all_owned():
    spec = _W3Spec()
    spec.balance_of[SENDER.lower()] = 2
    spec.token_owner_index[(SENDER.lower(), 0)] = 11
    spec.token_owner_index[(SENDER.lower(), 1)] = 22
    spec.positions_tuple = _default_positions_tuple()
    w3 = _make_w3_mock(spec)

    ps = get_positions(w3, SEPOLIA, SENDER)
    assert len(ps) == 2
    assert {p.token_id for p in ps} == {11, 22}


# --- prepare_collect -------------------------------------------------------


def test_prepare_collect_uses_max_uint128_and_sender_recipient():
    spec = _W3Spec()
    w3 = _make_w3_mock(spec)

    captured: dict[str, Any] = {}

    # Monkey-wrap collect to capture the params tuple it receives.
    original_factory = w3.eth.contract
    def wrap(address, abi):
        c = original_factory(address, abi)
        if address.lower() == NFPM_ADDR.lower():
            orig = c.functions.collect
            def collect_capture(params):
                captured["params"] = params
                return orig(params)
            c.functions.collect = collect_capture
        return c
    w3.eth.contract = wrap

    prepared = prepare_collect(w3, SEPOLIA, SENDER, token_id=99)

    assert prepared.description["kind"] == "uniswap_v3 lp_collect"
    assert prepared.description["lp_nft_token_id"] == 99
    assert prepared.description["lp_nfpm"].lower() == NFPM_ADDR.lower()
    assert prepared.description["to"].lower() == NFPM_ADDR.lower()
    # Tuple = (tokenId, recipient, amount0Max, amount1Max)
    assert captured["params"][2] == MAX_UINT128
    assert captured["params"][3] == MAX_UINT128
    assert captured["params"][1].lower() == SENDER.lower()


def test_prepare_collect_simulate_revert_surfaces_runtime_error():
    spec = _W3Spec()
    from web3.exceptions import ContractLogicError
    spec.simulate_raises = ContractLogicError("execution reverted: not approved")
    w3 = _make_w3_mock(spec)

    with pytest.raises(RuntimeError, match="simulation reverted"):
        prepare_collect(w3, SEPOLIA, SENDER, token_id=1)


# --- prepare_decrease_liquidity --------------------------------------------


def test_prepare_decrease_full_burn_carries_correct_fields():
    spec = _W3Spec()
    spec.positions_tuple = _default_positions_tuple(liquidity=10**18)
    w3 = _make_w3_mock(spec)

    prepared = prepare_decrease_liquidity(
        w3, SEPOLIA, SENDER, token_id=42, percent=100, slippage_bps=50,
    )

    desc = prepared.description
    assert desc["kind"] == "uniswap_v3 lp_decrease"
    assert desc["lp_action"] == "decrease"
    assert desc["lp_nft_token_id"] == 42
    assert desc["lp_liquidity_wei"] == 10**18  # 100% of liquidity
    # mins are floor of expected * (10000 - 50) / 10000, expected > 0 in range
    assert desc["lp_amount0_min_wei"] > 0
    assert desc["lp_amount0_min_wei"] < desc["lp_amount0_expected_wei"]
    assert desc["lp_amount1_min_wei"] < desc["lp_amount1_expected_wei"]
    assert desc["lp_percent"] == 100.0
    assert desc["lp_slippage_bps"] == 50


def test_prepare_decrease_partial_burn():
    spec = _W3Spec()
    spec.positions_tuple = _default_positions_tuple(liquidity=10**18)
    w3 = _make_w3_mock(spec)

    prepared = prepare_decrease_liquidity(
        w3, SEPOLIA, SENDER, token_id=42, percent=25, slippage_bps=100,
    )
    assert prepared.description["lp_liquidity_wei"] == 25 * 10**16


def test_prepare_decrease_rejects_percent_out_of_range():
    spec = _W3Spec()
    spec.positions_tuple = _default_positions_tuple()
    w3 = _make_w3_mock(spec)

    with pytest.raises(ValueError, match="percent"):
        prepare_decrease_liquidity(w3, SEPOLIA, SENDER, 1, percent=0, slippage_bps=50)
    with pytest.raises(ValueError, match="percent"):
        prepare_decrease_liquidity(w3, SEPOLIA, SENDER, 1, percent=101, slippage_bps=50)


def test_prepare_decrease_rejects_zero_liquidity_position():
    spec = _W3Spec()
    spec.positions_tuple = _default_positions_tuple(liquidity=0)
    w3 = _make_w3_mock(spec)

    with pytest.raises(ValueError, match="zero liquidity"):
        prepare_decrease_liquidity(w3, SEPOLIA, SENDER, 7, percent=50, slippage_bps=50)


# --- prepare_mint ----------------------------------------------------------


def _erc20_token(symbol: str, address: str, decimals: int) -> TokenInfo:
    return TokenInfo(symbol=symbol, address=Web3.to_checksum_address(address), decimals=decimals)


def test_prepare_mint_sorts_tokens_and_carries_description():
    # Pass token order swapped (WETH first, USDC second). The mint should
    # internally re-order to (USDC, WETH) because USDC address < WETH address
    # lexicographically (both happen to be 0x1... and 0xf...).
    spec = _W3Spec()
    spec.allowances[(USDC_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**12
    spec.allowances[(WETH_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    w3 = _make_w3_mock(spec)

    usdc = _erc20_token("USDC", USDC_ADDR, 6)
    weth = _erc20_token("WETH", WETH_ADDR, 18)

    prepared = prepare_mint(
        w3, SEPOLIA, SENDER,
        token_a=weth, amount_a_desired_wei=10**18,
        token_b=usdc, amount_b_desired_wei=10**9,
        fee=3000, tick_lower=-60, tick_upper=60, slippage_bps=50,
    )

    desc = prepared.description
    assert desc["kind"] == "uniswap_v3 lp_mint"
    # token0 must be the lex-smaller address — USDC's 0x1c… < WETH's 0xff…
    assert desc["lp_token0_address"].lower() == USDC_ADDR.lower()
    assert desc["lp_token1_address"].lower() == WETH_ADDR.lower()
    assert desc["lp_amount0_desired_wei"] == 10**9   # USDC side
    assert desc["lp_amount1_desired_wei"] == 10**18  # WETH side
    assert desc["lp_fee"] == 3000
    assert desc["lp_tick_lower"] == -60
    assert desc["lp_tick_upper"] == 60
    assert desc["lp_native_value_wei"] == 0  # ERC-20 only path


def test_prepare_mint_rejects_unaligned_ticks():
    spec = _W3Spec()
    spec.allowances[(USDC_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**12
    spec.allowances[(WETH_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    w3 = _make_w3_mock(spec)

    usdc = _erc20_token("USDC", USDC_ADDR, 6)
    weth = _erc20_token("WETH", WETH_ADDR, 18)

    with pytest.raises(ValueError, match="not aligned"):
        prepare_mint(
            w3, SEPOLIA, SENDER,
            token_a=usdc, amount_a_desired_wei=1,
            token_b=weth, amount_b_desired_wei=1,
            fee=3000, tick_lower=-55, tick_upper=60, slippage_bps=50,
        )


def test_prepare_mint_rejects_lower_ge_upper():
    spec = _W3Spec()
    spec.allowances[(USDC_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**12
    spec.allowances[(WETH_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    w3 = _make_w3_mock(spec)

    usdc = _erc20_token("USDC", USDC_ADDR, 6)
    weth = _erc20_token("WETH", WETH_ADDR, 18)

    with pytest.raises(ValueError, match="strictly less"):
        prepare_mint(
            w3, SEPOLIA, SENDER,
            token_a=usdc, amount_a_desired_wei=1,
            token_b=weth, amount_b_desired_wei=1,
            fee=3000, tick_lower=60, tick_upper=60, slippage_bps=50,
        )


def test_prepare_mint_insufficient_allowance_raises():
    spec = _W3Spec()
    # USDC allowance is zero; WETH unlimited
    spec.allowances[(WETH_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    w3 = _make_w3_mock(spec)

    usdc = _erc20_token("USDC", USDC_ADDR, 6)
    weth = _erc20_token("WETH", WETH_ADDR, 18)

    with pytest.raises(InsufficientAllowance):
        prepare_mint(
            w3, SEPOLIA, SENDER,
            token_a=usdc, amount_a_desired_wei=10**9,
            token_b=weth, amount_b_desired_wei=10**18,
            fee=3000, tick_lower=-60, tick_upper=60, slippage_bps=50,
        )


def test_prepare_mint_native_eth_routes_value_and_wraps_multicall():
    spec = _W3Spec()
    spec.allowances[(USDC_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**12
    w3 = _make_w3_mock(spec)

    usdc = _erc20_token("USDC", USDC_ADDR, 6)
    # Native ETH side — address points at WETH9 (calldata reference) but is_native=True
    eth_native = TokenInfo(
        symbol="ETH", address=Web3.to_checksum_address(WETH_ADDR),
        decimals=18, is_native=True,
    )

    prepared = prepare_mint(
        w3, SEPOLIA, SENDER,
        token_a=eth_native, amount_a_desired_wei=10**17,
        token_b=usdc, amount_b_desired_wei=10**9,
        fee=3000, tick_lower=-60, tick_upper=60, slippage_bps=50,
    )

    desc = prepared.description
    # WETH is the higher address; sort places it as token1. ETH side is token1.
    assert desc["lp_token0_address"].lower() == USDC_ADDR.lower()
    assert desc["lp_token1_address"].lower() == WETH_ADDR.lower()
    assert desc["lp_amount1_desired_wei"] == 10**17
    # tx.value carries the native ETH amount
    assert prepared.tx["value"] == 10**17
    assert desc["lp_native_value_wei"] == 10**17


def test_prepare_mint_rejects_identical_tokens():
    spec = _W3Spec()
    w3 = _make_w3_mock(spec)
    usdc = _erc20_token("USDC", USDC_ADDR, 6)
    with pytest.raises(ValueError, match="distinct"):
        prepare_mint(
            w3, SEPOLIA, SENDER,
            token_a=usdc, amount_a_desired_wei=1,
            token_b=usdc, amount_b_desired_wei=1,
            fee=3000, tick_lower=-60, tick_upper=60, slippage_bps=50,
        )


# --- prepare_increase_liquidity --------------------------------------------


def test_prepare_increase_matches_position_token_pair():
    spec = _W3Spec()
    spec.positions_tuple = _default_positions_tuple()
    spec.allowances[(USDC_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**12
    spec.allowances[(WETH_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    w3 = _make_w3_mock(spec)

    usdc = _erc20_token("USDC", USDC_ADDR, 6)
    weth = _erc20_token("WETH", WETH_ADDR, 18)

    prepared = prepare_increase_liquidity(
        w3, SEPOLIA, SENDER, token_id=42,
        token_a=usdc, amount_a_desired_wei=10**9,
        token_b=weth, amount_b_desired_wei=10**18,
        slippage_bps=50,
    )
    desc = prepared.description
    assert desc["kind"] == "uniswap_v3 lp_increase"
    assert desc["lp_nft_token_id"] == 42


def test_prepare_increase_rejects_wrong_token_pair():
    spec = _W3Spec()
    # Position holds USDC/WETH; we'll try to increase with a different token.
    spec.positions_tuple = _default_positions_tuple()
    fake_addr = "0x" + "ab" * 20
    spec.token_meta[fake_addr.lower()] = ("FAKE", 18)
    spec.allowances[(fake_addr.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    spec.allowances[(WETH_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    w3 = _make_w3_mock(spec)

    fake = _erc20_token("FAKE", fake_addr, 18)
    weth = _erc20_token("WETH", WETH_ADDR, 18)

    with pytest.raises(ValueError, match="mismatch"):
        prepare_increase_liquidity(
            w3, SEPOLIA, SENDER, token_id=42,
            token_a=fake, amount_a_desired_wei=1,
            token_b=weth, amount_b_desired_wei=1,
            slippage_bps=50,
        )


def test_nfpm_abi_includes_required_functions():
    """Lock the ABI surface so accidental edits to NFPM_ABI fail loudly."""
    fns = {entry["name"] for entry in NFPM_ABI if entry.get("type") == "function"}
    required = {
        "balanceOf", "tokenOfOwnerByIndex", "positions",
        "mint", "increaseLiquidity", "decreaseLiquidity",
        "collect", "multicall", "refundETH",
    }
    assert required.issubset(fns), f"missing: {required - fns}"

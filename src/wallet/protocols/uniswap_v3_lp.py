"""Uniswap V3 LP primitives — read positions + mint / increase / decrease / collect.

Mirrors the read/write split in `protocols.aave`: pure-read helpers
(`get_positions`, `fetch_position`) and write builders that return
`PreparedTx` (`prepare_mint`, `prepare_increase_liquidity`,
`prepare_decrease_liquidity`, `prepare_collect`).

Off-chain math (slippage min computations, in-range checks) routes through
`wallet.core.uniswap_v3_math`; the chain remains the source of truth.

Native-ETH inputs (one side of `mint` or `increase`) are supported by wrapping
the position-manager call in an NFPM `multicall([…, refundETH])` so any unused
ETH bounces back in the same tx. The `is_native` flag must come from the
CLI's native-symbol resolution path (see `cli/swap.py:_resolve_token_or_native`)
— never from a token's `symbol()` reading. (security_review.md Vuln 1.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from web3 import Web3

from wallet.core.config import ChainConfig, get_protocol_address
from wallet.core.tokens import (
    TokenInfo,
    allowance,
    fetch_token_info,
)
from wallet.core.tx import PreparedTx, _common_fields, _simulate, _strip_nonce
from wallet.core.uniswap_v3_math import (
    MAX_UINT128,
    align_to_tick_spacing,
    get_amounts_for_liquidity,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
    tick_spacing_for_fee,
)
from wallet.protocols.swap import InsufficientAllowance


# --- ABIs ------------------------------------------------------------------


NFPM_ABI: list[dict] = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "tokenOfOwnerByIndex",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "index", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "positions",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [
            {"name": "nonce", "type": "uint96"},
            {"name": "operator", "type": "address"},
            {"name": "token0", "type": "address"},
            {"name": "token1", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"name": "tokensOwed0", "type": "uint128"},
            {"name": "tokensOwed1", "type": "uint128"},
        ],
    },
    {
        "name": "mint",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "token0", "type": "address"},
                    {"name": "token1", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "tickLower", "type": "int24"},
                    {"name": "tickUpper", "type": "int24"},
                    {"name": "amount0Desired", "type": "uint256"},
                    {"name": "amount1Desired", "type": "uint256"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
                ],
            },
        ],
        "outputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "name": "increaseLiquidity",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "amount0Desired", "type": "uint256"},
                    {"name": "amount1Desired", "type": "uint256"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
            },
        ],
        "outputs": [
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "name": "decreaseLiquidity",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "liquidity", "type": "uint128"},
                    {"name": "amount0Min", "type": "uint256"},
                    {"name": "amount1Min", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
            },
        ],
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "name": "collect",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amount0Max", "type": "uint128"},
                    {"name": "amount1Max", "type": "uint128"},
                ],
            },
        ],
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "name": "multicall",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "data", "type": "bytes[]"}],
        "outputs": [{"name": "results", "type": "bytes[]"}],
    },
    {
        "name": "refundETH",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [],
        "outputs": [],
    },
]


UNISWAP_V3_POOL_ABI: list[dict] = [
    {
        "name": "slot0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
    },
]


UNISWAP_V3_FACTORY_ABI: list[dict] = [
    {
        "name": "getPool",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA", "type": "address"},
            {"name": "tokenB", "type": "address"},
            {"name": "fee", "type": "uint24"},
        ],
        "outputs": [{"name": "pool", "type": "address"}],
    },
]


# --- dataclasses -----------------------------------------------------------


@dataclass(frozen=True)
class LpPosition:
    """A single Uniswap V3 LP position as held by `owner` on NFPM."""

    token_id: int
    token0_address: str
    token1_address: str
    token0_symbol: str
    token1_symbol: str
    token0_decimals: int
    token1_decimals: int
    fee: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    tokens_owed0: int
    tokens_owed1: int
    pool_address: str
    current_sqrt_price_x96: int
    current_tick: int
    in_range: bool
    amount0_wei: int
    amount1_wei: int


# --- internal helpers ------------------------------------------------------


def _nfpm(w3, chain: ChainConfig):
    return w3.eth.contract(
        address=Web3.to_checksum_address(
            get_protocol_address(chain, "uniswap_v3", "nonfungible_position_manager")
        ),
        abi=NFPM_ABI,
    )


def _factory(w3, chain: ChainConfig):
    return w3.eth.contract(
        address=Web3.to_checksum_address(
            get_protocol_address(chain, "uniswap_v3", "factory")
        ),
        abi=UNISWAP_V3_FACTORY_ABI,
    )


def _pool(w3, address: str):
    return w3.eth.contract(
        address=Web3.to_checksum_address(address), abi=UNISWAP_V3_POOL_ABI
    )


def _apply_slippage_floor(amount: int, slippage_bps: int) -> int:
    """Compute amountMin given an expected amount and slippage in bps."""
    if slippage_bps < 0 or slippage_bps > 10_000:
        raise ValueError(f"slippage_bps must be in [0, 10000], got {slippage_bps}")
    return (amount * (10_000 - slippage_bps)) // 10_000


def _sort_token_pair(
    token_a: TokenInfo, amount_a: int, token_b: TokenInfo, amount_b: int
) -> tuple[TokenInfo, int, TokenInfo, int]:
    """Return (token0, amount0, token1, amount1) ordered by address ascending.

    Uniswap V3 NFPM rejects any (token0, token1) where token0 >= token1.
    The CLI accepts user-friendly input order; we sort here so the agent
    never has to remember which side is lexicographically smaller.
    """
    if int(token_a.address, 16) < int(token_b.address, 16):
        return token_a, amount_a, token_b, amount_b
    return token_b, amount_b, token_a, amount_a


# --- read: positions -------------------------------------------------------


def fetch_position(w3, chain: ChainConfig, token_id: int) -> LpPosition:
    """Read a single position from NFPM and enrich with pool state."""
    nfpm = _nfpm(w3, chain)
    raw = nfpm.functions.positions(int(token_id)).call()
    (
        _nonce, _operator, token0, token1, fee,
        tick_lower, tick_upper, liquidity,
        _fg0, _fg1, owed0, owed1,
    ) = raw

    info0 = fetch_token_info(w3, token0)
    info1 = fetch_token_info(w3, token1)

    factory = _factory(w3, chain)
    pool_addr = factory.functions.getPool(
        Web3.to_checksum_address(token0),
        Web3.to_checksum_address(token1),
        int(fee),
    ).call()

    pool = _pool(w3, pool_addr)
    slot0 = pool.functions.slot0().call()
    sqrt_price_x96 = int(slot0[0])
    current_tick = int(slot0[1])

    in_range = tick_lower <= current_tick < tick_upper
    sqrt_a = get_sqrt_ratio_at_tick(int(tick_lower))
    sqrt_b = get_sqrt_ratio_at_tick(int(tick_upper))
    amt0, amt1 = get_amounts_for_liquidity(sqrt_price_x96, sqrt_a, sqrt_b, int(liquidity))

    return LpPosition(
        token_id=int(token_id),
        token0_address=Web3.to_checksum_address(token0),
        token1_address=Web3.to_checksum_address(token1),
        token0_symbol=info0.symbol,
        token1_symbol=info1.symbol,
        token0_decimals=info0.decimals,
        token1_decimals=info1.decimals,
        fee=int(fee),
        tick_lower=int(tick_lower),
        tick_upper=int(tick_upper),
        liquidity=int(liquidity),
        tokens_owed0=int(owed0),
        tokens_owed1=int(owed1),
        pool_address=Web3.to_checksum_address(pool_addr),
        current_sqrt_price_x96=sqrt_price_x96,
        current_tick=current_tick,
        in_range=in_range,
        amount0_wei=int(amt0),
        amount1_wei=int(amt1),
    )


def get_positions(w3, chain: ChainConfig, owner: str) -> list[LpPosition]:
    """List all NFPM positions held by `owner`. Order matches NFPM enumeration."""
    nfpm = _nfpm(w3, chain)
    owner_cs = Web3.to_checksum_address(owner)
    count = int(nfpm.functions.balanceOf(owner_cs).call())
    positions: list[LpPosition] = []
    for i in range(count):
        token_id = int(nfpm.functions.tokenOfOwnerByIndex(owner_cs, i).call())
        positions.append(fetch_position(w3, chain, token_id))
    return positions


# --- write: prepare_collect ------------------------------------------------


def prepare_collect(
    w3, chain: ChainConfig, sender: str, token_id: int, recipient: str | None = None,
) -> PreparedTx:
    """Build an unsigned NFPM.collect() tx; sweeps owed fees + any decreased
    liquidity that hasn't been collected yet.

    Uses `MAX_UINT128` for amount0Max / amount1Max — the NFPM-canonical "take
    everything available" sentinel. `recipient` defaults to `sender`.
    """
    sender_cs = Web3.to_checksum_address(sender)
    recipient_cs = Web3.to_checksum_address(recipient) if recipient else sender_cs
    nfpm_addr = Web3.to_checksum_address(
        get_protocol_address(chain, "uniswap_v3", "nonfungible_position_manager")
    )

    nfpm = w3.eth.contract(address=nfpm_addr, abi=NFPM_ABI)
    base = _common_fields(w3, chain, sender)
    tx = nfpm.functions.collect(
        (int(token_id), recipient_cs, MAX_UINT128, MAX_UINT128),
    ).build_transaction(base)
    _simulate(w3, tx)
    _strip_nonce(tx)
    fee_wei = tx["maxFeePerGas"] * tx["gas"]

    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee_wei,
        description={
            "kind": "uniswap_v3 lp_collect",
            "from": tx["from"],
            "to": nfpm_addr,
            "amount_wei": 0,
            "amount_unit": "fees",
            "amount_decimals": 0,
            "lp_action": "collect",
            "lp_nft_token_id": int(token_id),
            "lp_nfpm": nfpm_addr,
            "lp_recipient": recipient_cs,
        },
    )


# --- write: prepare_decrease_liquidity -------------------------------------


def prepare_decrease_liquidity(
    w3,
    chain: ChainConfig,
    sender: str,
    token_id: int,
    percent: float,
    slippage_bps: int,
    deadline_seconds_from_now: int = 1800,
) -> PreparedTx:
    """Burn `percent`% of position liquidity. `amount0Min` / `amount1Min`
    derived from on-chain `slot0` + the position's tick range, then floored
    by `slippage_bps`.

    Note: decreaseLiquidity moves the proceeds into the NFPM-owed buckets;
    the user must follow this with a `collect` to withdraw. We do NOT
    auto-chain those — one CLI command, one on-chain side effect, so policy
    / audit / idempotency see each step.
    """
    import time

    if not (0 < percent <= 100):
        raise ValueError(f"percent must be in (0, 100], got {percent}")

    pos = fetch_position(w3, chain, token_id)
    if pos.liquidity == 0:
        raise ValueError(
            f"position {token_id} has zero liquidity — nothing to decrease "
            "(use `wallet lp collect` to sweep owed amounts)"
        )

    liquidity_to_burn = (pos.liquidity * int(percent * 100)) // 10_000
    if liquidity_to_burn == 0:
        raise ValueError(
            f"percent={percent} of liquidity={pos.liquidity} rounds to zero"
        )

    sqrt_a = get_sqrt_ratio_at_tick(pos.tick_lower)
    sqrt_b = get_sqrt_ratio_at_tick(pos.tick_upper)
    expected_amt0, expected_amt1 = get_amounts_for_liquidity(
        pos.current_sqrt_price_x96, sqrt_a, sqrt_b, liquidity_to_burn
    )
    amount0_min = _apply_slippage_floor(expected_amt0, slippage_bps)
    amount1_min = _apply_slippage_floor(expected_amt1, slippage_bps)

    deadline = int(time.time()) + int(deadline_seconds_from_now)

    nfpm_addr = Web3.to_checksum_address(
        get_protocol_address(chain, "uniswap_v3", "nonfungible_position_manager")
    )
    nfpm = w3.eth.contract(address=nfpm_addr, abi=NFPM_ABI)
    base = _common_fields(w3, chain, sender)
    tx = nfpm.functions.decreaseLiquidity(
        (int(token_id), int(liquidity_to_burn), int(amount0_min), int(amount1_min), int(deadline)),
    ).build_transaction(base)
    _simulate(w3, tx)
    _strip_nonce(tx)
    fee_wei = tx["maxFeePerGas"] * tx["gas"]

    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee_wei,
        description={
            "kind": "uniswap_v3 lp_decrease",
            "from": tx["from"],
            "to": nfpm_addr,
            "amount_wei": int(liquidity_to_burn),
            "amount_unit": "liquidity",
            "amount_decimals": 0,
            "lp_action": "decrease",
            "lp_nft_token_id": int(token_id),
            "lp_nfpm": nfpm_addr,
            "lp_token0_address": pos.token0_address,
            "lp_token1_address": pos.token1_address,
            "lp_fee": pos.fee,
            "lp_tick_lower": pos.tick_lower,
            "lp_tick_upper": pos.tick_upper,
            "lp_liquidity_wei": int(liquidity_to_burn),
            "lp_amount0_expected_wei": int(expected_amt0),
            "lp_amount1_expected_wei": int(expected_amt1),
            "lp_amount0_min_wei": int(amount0_min),
            "lp_amount1_min_wei": int(amount1_min),
            "lp_slippage_bps": int(slippage_bps),
            "lp_percent": float(percent),
        },
    )


# --- write: prepare_mint ---------------------------------------------------


def _check_allowance_or_raise(
    w3, token: TokenInfo, sender_cs: str, spender_cs: str, required_wei: int
) -> None:
    """Pre-flight allowance gate. Skipped for native ETH inputs (msg.value path)."""
    if token.is_native:
        return
    if required_wei == 0:
        return
    current = allowance(w3, token.address, sender_cs, spender_cs)
    if current < required_wei:
        raise InsufficientAllowance(
            token_symbol=token.symbol,
            token_address=token.address,
            spender=spender_cs,
            current_wei=current,
            required_wei=required_wei,
        )


def _maybe_multicall_with_refund(
    nfpm, action_call_data: bytes, native_value_wei: int
) -> tuple[str, int]:
    """If the call carries native ETH, wrap it as `multicall([action, refundETH])`
    so any unused ETH (from `amountDesired` not being fully consumed) bounces
    back to `sender` in the same tx. Returns (calldata, value_wei) to splice
    into the tx.

    Without `refundETH`, NFPM keeps the leftover ETH balance — which would
    silently donate it to the next caller.
    """
    if native_value_wei <= 0:
        return action_call_data, 0
    refund_data = nfpm.encode_abi("refundETH", args=[])
    multicall_data = nfpm.encode_abi("multicall", args=[[action_call_data, refund_data]])
    return multicall_data, native_value_wei


def prepare_mint(
    w3,
    chain: ChainConfig,
    sender: str,
    token_a: TokenInfo,
    amount_a_desired_wei: int,
    token_b: TokenInfo,
    amount_b_desired_wei: int,
    fee: int,
    tick_lower: int,
    tick_upper: int,
    slippage_bps: int,
    deadline_seconds_from_now: int = 1800,
) -> PreparedTx:
    """Open a new LP position.

    `token_a / token_b` order is user-friendly; we sort internally to
    Uniswap's required (token0 < token1). `tick_lower / tick_upper` must
    be pre-aligned to the fee tier's tickSpacing — we reject rather than
    silently round, so the agent's intent is preserved (a rounded position
    has different economics).

    Native ETH on one side (set by the CLI via `TokenInfo.is_native=True`)
    flows as `msg.value`; the calldata still references WETH and we attach
    `refundETH` via NFPM `multicall` so unused ETH bounces back atomically.
    """
    import time

    if token_a.address.lower() == token_b.address.lower():
        raise ValueError(
            f"mint requires two distinct tokens; both are {token_a.symbol}"
        )

    spacing = tick_spacing_for_fee(fee)
    if not align_to_tick_spacing(tick_lower, spacing):
        raise ValueError(
            f"tick_lower={tick_lower} not aligned to spacing={spacing} for fee={fee}; "
            f"adjust to a multiple of {spacing}"
        )
    if not align_to_tick_spacing(tick_upper, spacing):
        raise ValueError(
            f"tick_upper={tick_upper} not aligned to spacing={spacing} for fee={fee}; "
            f"adjust to a multiple of {spacing}"
        )
    if tick_lower >= tick_upper:
        raise ValueError(
            f"tick_lower ({tick_lower}) must be strictly less than tick_upper ({tick_upper})"
        )

    token0, amount0_desired, token1, amount1_desired = _sort_token_pair(
        token_a, amount_a_desired_wei, token_b, amount_b_desired_wei
    )
    if token0.is_native and token1.is_native:
        raise ValueError("both sides cannot be native ETH")

    sender_cs = Web3.to_checksum_address(sender)
    nfpm_addr = Web3.to_checksum_address(
        get_protocol_address(chain, "uniswap_v3", "nonfungible_position_manager")
    )

    # Pre-flight allowance for each non-native leg.
    _check_allowance_or_raise(w3, token0, sender_cs, nfpm_addr, amount0_desired)
    _check_allowance_or_raise(w3, token1, sender_cs, nfpm_addr, amount1_desired)

    amount0_min = _apply_slippage_floor(amount0_desired, slippage_bps)
    amount1_min = _apply_slippage_floor(amount1_desired, slippage_bps)
    deadline = int(time.time()) + int(deadline_seconds_from_now)

    nfpm = w3.eth.contract(address=nfpm_addr, abi=NFPM_ABI)
    mint_data = nfpm.encode_abi(
        "mint",
        args=[(
            Web3.to_checksum_address(token0.address),
            Web3.to_checksum_address(token1.address),
            int(fee),
            int(tick_lower),
            int(tick_upper),
            int(amount0_desired),
            int(amount1_desired),
            int(amount0_min),
            int(amount1_min),
            sender_cs,
            int(deadline),
        )],
    )

    native_value = 0
    if token0.is_native:
        native_value = int(amount0_desired)
    elif token1.is_native:
        native_value = int(amount1_desired)

    calldata, value_wei = _maybe_multicall_with_refund(nfpm, mint_data, native_value)

    base = _common_fields(w3, chain, sender)
    tx: dict[str, Any] = {
        **base,
        "to": nfpm_addr,
        "value": value_wei,
        "data": calldata,
    }
    tx["gas"] = w3.eth.estimate_gas(tx)
    _simulate(w3, tx)
    _strip_nonce(tx)
    fee_wei = tx["maxFeePerGas"] * tx["gas"]

    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee_wei,
        description={
            "kind": "uniswap_v3 lp_mint",
            "from": tx["from"],
            "to": nfpm_addr,
            "amount_wei": int(amount0_desired),
            "amount_unit": token0.symbol,
            "amount_decimals": token0.decimals,
            "lp_action": "mint",
            "lp_nfpm": nfpm_addr,
            "lp_token0_address": Web3.to_checksum_address(token0.address),
            "lp_token1_address": Web3.to_checksum_address(token1.address),
            "lp_token0_symbol": token0.symbol,
            "lp_token1_symbol": token1.symbol,
            "lp_token0_decimals": token0.decimals,
            "lp_token1_decimals": token1.decimals,
            "lp_fee": int(fee),
            "lp_tick_lower": int(tick_lower),
            "lp_tick_upper": int(tick_upper),
            "lp_amount0_desired_wei": int(amount0_desired),
            "lp_amount1_desired_wei": int(amount1_desired),
            "lp_amount0_min_wei": int(amount0_min),
            "lp_amount1_min_wei": int(amount1_min),
            "lp_slippage_bps": int(slippage_bps),
            "lp_native_value_wei": int(native_value),
        },
    )


# --- write: prepare_increase_liquidity -------------------------------------


def prepare_increase_liquidity(
    w3,
    chain: ChainConfig,
    sender: str,
    token_id: int,
    token_a: TokenInfo,
    amount_a_desired_wei: int,
    token_b: TokenInfo,
    amount_b_desired_wei: int,
    slippage_bps: int,
    deadline_seconds_from_now: int = 1800,
) -> PreparedTx:
    """Add more liquidity to an existing position.

    Validates that the supplied (token_a, token_b) pair matches the position's
    on-chain (token0, token1) — otherwise NFPM would silently mis-route
    funds. Same is_native + multicall+refundETH pattern as `prepare_mint`.
    """
    import time

    pos = fetch_position(w3, chain, token_id)
    token0, amount0_desired, token1, amount1_desired = _sort_token_pair(
        token_a, amount_a_desired_wei, token_b, amount_b_desired_wei
    )
    if token0.address.lower() != pos.token0_address.lower():
        raise ValueError(
            f"token0 mismatch: position {token_id} has {pos.token0_address}, "
            f"got {token0.address}"
        )
    if token1.address.lower() != pos.token1_address.lower():
        raise ValueError(
            f"token1 mismatch: position {token_id} has {pos.token1_address}, "
            f"got {token1.address}"
        )
    if token0.is_native and token1.is_native:
        raise ValueError("both sides cannot be native ETH")

    sender_cs = Web3.to_checksum_address(sender)
    nfpm_addr = Web3.to_checksum_address(
        get_protocol_address(chain, "uniswap_v3", "nonfungible_position_manager")
    )

    _check_allowance_or_raise(w3, token0, sender_cs, nfpm_addr, amount0_desired)
    _check_allowance_or_raise(w3, token1, sender_cs, nfpm_addr, amount1_desired)

    amount0_min = _apply_slippage_floor(amount0_desired, slippage_bps)
    amount1_min = _apply_slippage_floor(amount1_desired, slippage_bps)
    deadline = int(time.time()) + int(deadline_seconds_from_now)

    nfpm = w3.eth.contract(address=nfpm_addr, abi=NFPM_ABI)
    inc_data = nfpm.encode_abi(
        "increaseLiquidity",
        args=[(
            int(token_id),
            int(amount0_desired),
            int(amount1_desired),
            int(amount0_min),
            int(amount1_min),
            int(deadline),
        )],
    )

    native_value = 0
    if token0.is_native:
        native_value = int(amount0_desired)
    elif token1.is_native:
        native_value = int(amount1_desired)

    calldata, value_wei = _maybe_multicall_with_refund(nfpm, inc_data, native_value)

    base = _common_fields(w3, chain, sender)
    tx: dict[str, Any] = {
        **base,
        "to": nfpm_addr,
        "value": value_wei,
        "data": calldata,
    }
    tx["gas"] = w3.eth.estimate_gas(tx)
    _simulate(w3, tx)
    _strip_nonce(tx)
    fee_wei = tx["maxFeePerGas"] * tx["gas"]

    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee_wei,
        description={
            "kind": "uniswap_v3 lp_increase",
            "from": tx["from"],
            "to": nfpm_addr,
            "amount_wei": int(amount0_desired),
            "amount_unit": token0.symbol,
            "amount_decimals": token0.decimals,
            "lp_action": "increase",
            "lp_nft_token_id": int(token_id),
            "lp_nfpm": nfpm_addr,
            "lp_token0_address": Web3.to_checksum_address(token0.address),
            "lp_token1_address": Web3.to_checksum_address(token1.address),
            "lp_token0_symbol": token0.symbol,
            "lp_token1_symbol": token1.symbol,
            "lp_token0_decimals": token0.decimals,
            "lp_token1_decimals": token1.decimals,
            "lp_fee": pos.fee,
            "lp_tick_lower": pos.tick_lower,
            "lp_tick_upper": pos.tick_upper,
            "lp_amount0_desired_wei": int(amount0_desired),
            "lp_amount1_desired_wei": int(amount1_desired),
            "lp_amount0_min_wei": int(amount0_min),
            "lp_amount1_min_wei": int(amount1_min),
            "lp_slippage_bps": int(slippage_bps),
            "lp_native_value_wei": int(native_value),
        },
    )


# --- in-range helper (exported so the CLI / agent can interpret slot0) ----


def is_in_range(current_tick: int, tick_lower: int, tick_upper: int) -> bool:
    """Uniswap V3's canonical in-range predicate: `tick_lower <= current < tick_upper`."""
    return tick_lower <= current_tick < tick_upper


def slot0_in_range(sqrt_price_x96: int, tick_lower: int, tick_upper: int) -> bool:
    """Same predicate using sqrtPriceX96 instead of a chain-reported tick."""
    current = get_tick_at_sqrt_ratio(sqrt_price_x96)
    return is_in_range(current, tick_lower, tick_upper)

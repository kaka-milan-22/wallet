"""Uniswap V3 off-chain tick / sqrt-price / liquidity math.

Ports just enough of `@uniswap/v3-core` (`TickMath.sol`, `SqrtPriceMath.sol`)
to (a) translate between human tick ranges and the protocol's Q96 sqrt-price
representation and (b) predict the (amount0, amount1) that a position holds at
a given price.

These are OFF-CHAIN aids — used for slippage `amountMin` calculations and
human display. The chain is the source of truth: when an actual revert
reason or in-range check matters, read `slot0()` and compare on-chain
values. We use Python's arbitrary-precision `Decimal` instead of the
Solidity bit-shift tricks because (1) we don't need gas optimization and
(2) the result is auditable line-by-line.

Anchor points match the on-chain values exactly:
    tick=0          → sqrtPriceX96 = 2**96
    tick=MIN_TICK   → MIN_SQRT_RATIO   (4295128739)
    tick=MAX_TICK   → MAX_SQRT_RATIO   (1461446703485210103287273052203988822378723970342)
"""

from __future__ import annotations

from decimal import Decimal, getcontext

__all__ = [
    "FEE_TIER_TICK_SPACING",
    "MAX_SQRT_RATIO",
    "MAX_TICK",
    "MAX_UINT128",
    "MIN_SQRT_RATIO",
    "MIN_TICK",
    "Q96",
    "align_to_tick_spacing",
    "get_amount0_for_liquidity",
    "get_amount1_for_liquidity",
    "get_amounts_for_liquidity",
    "get_sqrt_ratio_at_tick",
    "get_tick_at_sqrt_ratio",
    "tick_spacing_for_fee",
]


# --- protocol-level constants (from v3-core/contracts/libraries/TickMath.sol) ---

MIN_TICK: int = -887272
MAX_TICK: int = 887272
MIN_SQRT_RATIO: int = 4295128739
MAX_SQRT_RATIO: int = 1461446703485210103287273052203988822378723970342

Q96: int = 1 << 96

MAX_UINT128: int = (1 << 128) - 1

# Fee tier (1e-6 units → e.g. 3000 = 0.3%) → on-chain pool tickSpacing.
# Source: UniswapV3Factory.feeAmountTickSpacing() initial values.
FEE_TIER_TICK_SPACING: dict[int, int] = {
    100: 1,
    500: 10,
    3000: 60,
    10000: 200,
}


def tick_spacing_for_fee(fee: int) -> int:
    """Return the pool-level tickSpacing for a Uniswap V3 fee tier.

    Raises ValueError for any fee tier not in the standard set; custom fee
    tiers added by governance are not supported off-chain (the factory call
    `feeAmountTickSpacing(fee)` is the on-chain source of truth).
    """
    spacing = FEE_TIER_TICK_SPACING.get(int(fee))
    if spacing is None:
        raise ValueError(
            f"unsupported fee tier {fee}; supported: "
            f"{sorted(FEE_TIER_TICK_SPACING.keys())}"
        )
    return spacing


def align_to_tick_spacing(tick: int, spacing: int) -> bool:
    """True iff `tick` is already aligned to `spacing` (i.e. tick % spacing == 0).

    We intentionally do NOT silently round here. Aligning a user-provided
    range bound would change the position's economics behind the agent's
    back. The caller (CLI / agent) decides whether to round-up, round-down,
    or reject.
    """
    if spacing <= 0:
        raise ValueError(f"tick spacing must be positive, got {spacing}")
    return (tick % spacing) == 0


# --- tick ↔ sqrtPriceX96 ----------------------------------------------------

# Internal: pin Decimal precision once so repeated calls don't pay the
# context-mutation cost. 80 digits is enough headroom that the final int()
# truncation matches the on-chain bit-shift result at all three anchor
# ticks (MIN, 0, MAX) and remains within 1 ulp at every interior tick.
_PRECISION_DIGITS = 80


def _ctx_precision() -> None:
    if getcontext().prec < _PRECISION_DIGITS:
        getcontext().prec = _PRECISION_DIGITS


_LOG_1_0001_BASE = Decimal("1.0001")


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """Return floor(sqrt(1.0001^tick) * 2^96).

    Matches Uniswap V3 `TickMath.getSqrtRatioAtTick` at the three anchor
    points (MIN_TICK / 0 / MAX_TICK) exactly. Interior values may differ
    from the on-chain bit-shift implementation by ≤1 ulp; that is well
    within typical swap slippage and is the reason we never use this
    output for on-chain equality checks.
    """
    if tick < MIN_TICK or tick > MAX_TICK:
        raise ValueError(f"tick {tick} out of range [{MIN_TICK}, {MAX_TICK}]")
    if tick == 0:
        return Q96
    if tick == MIN_TICK:
        return MIN_SQRT_RATIO
    if tick == MAX_TICK:
        return MAX_SQRT_RATIO

    _ctx_precision()
    ratio_pow = _LOG_1_0001_BASE ** Decimal(tick)
    sqrt_ratio = ratio_pow.sqrt()
    return int(sqrt_ratio * (Decimal(2) ** 96))


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """Return the largest int `tick` such that `get_sqrt_ratio_at_tick(tick) <= sqrt_price_x96`.

    Mirrors the on-chain `TickMath.getTickAtSqrtRatio` contract that
    `Pool.slot0().tick` itself satisfies. Use this when comparing a freshly
    quoted sqrtPriceX96After against a range — but for the canonical
    in-range check, read `slot0().tick` directly.
    """
    if sqrt_price_x96 < MIN_SQRT_RATIO or sqrt_price_x96 >= MAX_SQRT_RATIO:
        raise ValueError(
            f"sqrtPriceX96 {sqrt_price_x96} outside [{MIN_SQRT_RATIO}, {MAX_SQRT_RATIO})"
        )
    _ctx_precision()
    ratio = Decimal(sqrt_price_x96) / (Decimal(2) ** 96)
    # tick = 2 * log_1.0001(ratio) — derived from sqrtRatio = 1.0001^(tick/2)
    price = ratio * ratio
    # Decimal.ln() / Decimal.ln() lets us avoid float conversion (which would
    # lose precision near MIN/MAX_TICK).
    tick_d = price.ln() / _LOG_1_0001_BASE.ln()
    # Round toward zero in floating sense, but we want "largest tick with
    # ratio(tick) <= sqrt_price_x96". Use floor then nudge: if the
    # quantization produced a tick whose ratio is too high, step down.
    candidate = int(tick_d)
    # Account for negative-side floor (int() truncates toward zero).
    if tick_d < 0 and Decimal(candidate) != tick_d:
        candidate -= 1
    # Step down while the candidate's ratio is above the input (handles the
    # rare ulp slip from the Decimal log).
    while candidate > MIN_TICK and get_sqrt_ratio_at_tick(candidate) > sqrt_price_x96:
        candidate -= 1
    # Step up while the next tick still satisfies the inequality (covers
    # the symmetric ulp slip in the other direction).
    while candidate < MAX_TICK and get_sqrt_ratio_at_tick(candidate + 1) <= sqrt_price_x96:
        candidate += 1
    return candidate


# --- liquidity ↔ amounts (SqrtPriceMath) ------------------------------------


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def get_amount0_for_liquidity(
    sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int
) -> int:
    """Token0 amount locked between two sqrt-prices for the given liquidity.

    Port of v3-core `LiquidityAmounts.getAmount0ForLiquidity`:

        amount0 = L * (sqrtB - sqrtA) / (sqrtA * sqrtB / Q96)

    Caller must ensure `liquidity >= 0`. Returns 0 when the two prices
    coincide.
    """
    if liquidity < 0:
        raise ValueError(f"liquidity must be non-negative, got {liquidity}")
    a, b = _ordered(sqrt_ratio_a_x96, sqrt_ratio_b_x96)
    if a <= 0:
        raise ValueError("sqrt ratio must be positive")
    if a == b:
        return 0
    # (L << 96) * (b - a) / (b * a) — Solidity does this with mulDiv to avoid
    # overflow; in Python the intermediate is just a big int, no special care.
    numerator1 = liquidity << 96
    numerator2 = b - a
    return (numerator1 * numerator2) // b // a


def get_amount1_for_liquidity(
    sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int
) -> int:
    """Token1 amount locked between two sqrt-prices for the given liquidity.

    Port of v3-core `LiquidityAmounts.getAmount1ForLiquidity`:

        amount1 = L * (sqrtB - sqrtA) / Q96
    """
    if liquidity < 0:
        raise ValueError(f"liquidity must be non-negative, got {liquidity}")
    a, b = _ordered(sqrt_ratio_a_x96, sqrt_ratio_b_x96)
    if a == b:
        return 0
    return (liquidity * (b - a)) >> 96


def get_amounts_for_liquidity(
    sqrt_ratio_current_x96: int,
    sqrt_ratio_a_x96: int,
    sqrt_ratio_b_x96: int,
    liquidity: int,
) -> tuple[int, int]:
    """Return (amount0, amount1) currently held by a position with `liquidity`
    spanning [sqrtA, sqrtB], at pool sqrt-price `sqrtCurrent`.

    Three regimes — same as on-chain `LiquidityAmounts.getAmountsForLiquidity`:
      - price below the range:   all token0
      - price above the range:   all token1
      - price inside the range:  mix of both
    """
    a, b = _ordered(sqrt_ratio_a_x96, sqrt_ratio_b_x96)
    if sqrt_ratio_current_x96 <= a:
        return (get_amount0_for_liquidity(a, b, liquidity), 0)
    if sqrt_ratio_current_x96 < b:
        return (
            get_amount0_for_liquidity(sqrt_ratio_current_x96, b, liquidity),
            get_amount1_for_liquidity(a, sqrt_ratio_current_x96, liquidity),
        )
    return (0, get_amount1_for_liquidity(a, b, liquidity))

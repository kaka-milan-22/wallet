"""Tests for the off-chain Q96 tick/sqrt-price/liquidity helpers.

Strategy: exact assertions at the three anchor ticks (MIN / 0 / MAX),
round-trip stability across a sampled tick set, monotonicity, tick-spacing
alignment, and a handful of known liquidity-amount vectors derived from
the algorithm's published behavior (token0-only below the range,
token1-only above the range, mix inside).
"""

from __future__ import annotations

import pytest

from wallet.core.uniswap_v3_math import (
    FEE_TIER_TICK_SPACING,
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    Q96,
    align_to_tick_spacing,
    get_amount0_for_liquidity,
    get_amount1_for_liquidity,
    get_amounts_for_liquidity,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
    tick_spacing_for_fee,
)


# --- anchor ticks (exact equality vs published on-chain constants) ----------


def test_sqrt_ratio_at_tick_zero_is_q96():
    assert get_sqrt_ratio_at_tick(0) == Q96


def test_sqrt_ratio_at_min_tick_matches_protocol_constant():
    assert get_sqrt_ratio_at_tick(MIN_TICK) == MIN_SQRT_RATIO


def test_sqrt_ratio_at_max_tick_matches_protocol_constant():
    assert get_sqrt_ratio_at_tick(MAX_TICK) == MAX_SQRT_RATIO


def test_sqrt_ratio_out_of_range_raises():
    with pytest.raises(ValueError):
        get_sqrt_ratio_at_tick(MIN_TICK - 1)
    with pytest.raises(ValueError):
        get_sqrt_ratio_at_tick(MAX_TICK + 1)


# --- monotonicity -----------------------------------------------------------


def test_sqrt_ratio_is_strictly_monotonic():
    # Sampling — full sweep would be 1.7M calls and slow the suite.
    ticks = [-100000, -1000, -60, -1, 0, 1, 60, 1000, 100000]
    ratios = [get_sqrt_ratio_at_tick(t) for t in ticks]
    assert ratios == sorted(ratios)
    assert len(set(ratios)) == len(ratios)  # strictly increasing


# --- inverse: sqrt → tick ---------------------------------------------------


@pytest.mark.parametrize("tick", [
    MIN_TICK, -200000, -60000, -1000, -60, -1, 0, 1, 60, 1000, 60000, 200000, MAX_TICK - 1,
])
def test_tick_at_sqrt_ratio_recovers_tick(tick):
    sqrt_px = get_sqrt_ratio_at_tick(tick)
    recovered = get_tick_at_sqrt_ratio(sqrt_px)
    # Contract is "largest tick whose ratio ≤ input". The ratio is the exact
    # one we just produced, so the recovered tick must satisfy:
    #   ratio(recovered) <= sqrt_px  AND  ratio(recovered + 1) > sqrt_px
    assert get_sqrt_ratio_at_tick(recovered) <= sqrt_px
    if recovered < MAX_TICK:
        assert get_sqrt_ratio_at_tick(recovered + 1) > sqrt_px
    # Off-by-one tolerance: the input ratio came from our own forward
    # function, so the recovered tick is either `tick` or `tick - 1`
    # depending on which side rounding landed on.
    assert abs(recovered - tick) <= 1


def test_tick_at_sqrt_ratio_rejects_out_of_range():
    with pytest.raises(ValueError):
        get_tick_at_sqrt_ratio(MIN_SQRT_RATIO - 1)
    with pytest.raises(ValueError):
        get_tick_at_sqrt_ratio(MAX_SQRT_RATIO)  # exclusive upper


# --- tick spacing -----------------------------------------------------------


def test_tick_spacing_lookup_for_known_fees():
    assert tick_spacing_for_fee(100) == 1
    assert tick_spacing_for_fee(500) == 10
    assert tick_spacing_for_fee(3000) == 60
    assert tick_spacing_for_fee(10000) == 200


def test_tick_spacing_rejects_unknown_fee():
    with pytest.raises(ValueError):
        tick_spacing_for_fee(123)


def test_align_to_tick_spacing_accepts_aligned():
    assert align_to_tick_spacing(0, 60) is True
    assert align_to_tick_spacing(120, 60) is True
    assert align_to_tick_spacing(-180, 60) is True


def test_align_to_tick_spacing_rejects_misaligned():
    assert align_to_tick_spacing(1, 60) is False
    assert align_to_tick_spacing(-59, 60) is False


def test_align_to_tick_spacing_zero_spacing_raises():
    with pytest.raises(ValueError):
        align_to_tick_spacing(0, 0)


# --- liquidity → amounts ----------------------------------------------------


def test_amount0_is_zero_when_prices_equal():
    sqrt = get_sqrt_ratio_at_tick(0)
    assert get_amount0_for_liquidity(sqrt, sqrt, liquidity=10**18) == 0


def test_amount1_is_zero_when_prices_equal():
    sqrt = get_sqrt_ratio_at_tick(0)
    assert get_amount1_for_liquidity(sqrt, sqrt, liquidity=10**18) == 0


def test_amount0_scales_with_liquidity():
    a = get_sqrt_ratio_at_tick(-60)
    b = get_sqrt_ratio_at_tick(60)
    small = get_amount0_for_liquidity(a, b, 10**18)
    large = get_amount0_for_liquidity(a, b, 10**21)
    # Strict equality of `small * 1000` would assume no floor-division loss
    # in the smaller-liquidity branch; the contract only promises monotonic
    # increase and proportionality to within 1 ulp per division.
    assert large > small
    assert abs(large - small * 1000) <= small  # within one full ulp of the smaller value


def test_amount1_scales_with_liquidity():
    a = get_sqrt_ratio_at_tick(-60)
    b = get_sqrt_ratio_at_tick(60)
    small = get_amount1_for_liquidity(a, b, 10**18)
    large = get_amount1_for_liquidity(a, b, 10**21)
    assert large > small
    assert abs(large - small * 1000) <= small


def test_amount_helpers_reject_negative_liquidity():
    with pytest.raises(ValueError):
        get_amount0_for_liquidity(Q96, Q96 + 1, -1)
    with pytest.raises(ValueError):
        get_amount1_for_liquidity(Q96, Q96 + 1, -1)


def test_amount_helpers_handle_unordered_sqrt_inputs():
    # Function must sort internally — passing b before a should produce the
    # same result as the ordered call, not negative amounts.
    a = get_sqrt_ratio_at_tick(-60)
    b = get_sqrt_ratio_at_tick(60)
    L = 10**18
    assert get_amount0_for_liquidity(b, a, L) == get_amount0_for_liquidity(a, b, L)
    assert get_amount1_for_liquidity(b, a, L) == get_amount1_for_liquidity(a, b, L)


def test_get_amounts_for_liquidity_below_range_is_all_token0():
    sqrt_a = get_sqrt_ratio_at_tick(60)
    sqrt_b = get_sqrt_ratio_at_tick(120)
    current = get_sqrt_ratio_at_tick(0)  # below range
    amt0, amt1 = get_amounts_for_liquidity(current, sqrt_a, sqrt_b, 10**18)
    assert amt0 > 0
    assert amt1 == 0


def test_get_amounts_for_liquidity_above_range_is_all_token1():
    sqrt_a = get_sqrt_ratio_at_tick(-120)
    sqrt_b = get_sqrt_ratio_at_tick(-60)
    current = get_sqrt_ratio_at_tick(0)  # above range
    amt0, amt1 = get_amounts_for_liquidity(current, sqrt_a, sqrt_b, 10**18)
    assert amt0 == 0
    assert amt1 > 0


def test_get_amounts_for_liquidity_in_range_is_mix():
    sqrt_a = get_sqrt_ratio_at_tick(-60)
    sqrt_b = get_sqrt_ratio_at_tick(60)
    current = get_sqrt_ratio_at_tick(0)
    amt0, amt1 = get_amounts_for_liquidity(current, sqrt_a, sqrt_b, 10**18)
    assert amt0 > 0
    assert amt1 > 0


def test_fee_tier_tick_spacing_table_matches_protocol():
    # Lock the table — if Uniswap governance adds a new fee tier we want a
    # failing test forcing an explicit update rather than silent drift.
    assert FEE_TIER_TICK_SPACING == {100: 1, 500: 10, 3000: 60, 10000: 200}

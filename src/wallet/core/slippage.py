"""Slippage helper — one source of truth for `amount_min = amount * (1 - bps)`.

Used by every DEX / LP prepare_* helper that takes a `slippage_bps` argument
and needs to compute the on-chain minimum (swap min-out, mint min-amounts,
decrease min-amounts). Kept in `core/` so route adapters and LP code share
the same edge-case handling — previously each had its own copy.

bps semantics: 50 bps = 0.50%. Range [0, 10_000] inclusive; out-of-range
raises ValueError rather than silently clamping, so config bugs surface
immediately rather than producing a permissive min-out.
"""

from __future__ import annotations

__all__ = ["apply_slippage_floor"]


def apply_slippage_floor(amount: int, slippage_bps: int) -> int:
    """Return `amount * (10_000 - slippage_bps) // 10_000`.

    Integer floor division matches what the Solidity side computes when
    checking `amountReceived >= amountMin`, so off-by-one rounding can't
    push a borderline successful tx into a revert.
    """
    if slippage_bps < 0 or slippage_bps > 10_000:
        raise ValueError(f"slippage_bps must be in [0, 10000], got {slippage_bps}")
    return (amount * (10_000 - slippage_bps)) // 10_000

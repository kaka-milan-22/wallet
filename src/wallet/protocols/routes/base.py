"""Route provider abstraction.

Concrete providers (direct Uniswap V3, 0x aggregator, 1inch, ...) implement
`quote(...)` to return a uniform `Quote` describing the swap tx to build.
`protocols/swap.py:prepare_swap` consumes a `Quote` and produces a `PreparedTx`
that flows through the existing `confirm_and_broadcast` pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo


@dataclass(frozen=True)
class Quote:
    """Uniform output across all route providers."""

    route_provider: str  # e.g. "uniswap_v3", "0x"
    route_description: str  # human-readable: "USDC > 500bps > WETH"

    to: str  # contract to call (router address)
    data: str  # hex calldata, with leading "0x"
    value: int  # native ETH value in wei (non-zero only when token_in is ETH)

    token_in_address: str
    token_out_address: str
    token_in_symbol: str
    token_out_symbol: str
    token_in_decimals: int
    token_out_decimals: int

    amount_in_wei: int
    amount_out_expected_wei: int  # what the route says you'll get pre-slippage
    amount_out_min_wei: int  # after slippage_bps applied — encoded in calldata

    spender: str  # address that needs ERC-20 allowance (usually == to)
    gas_estimate: int | None = None  # provider hint; wallet will eth_estimateGas as ground truth


class NoRouteError(RuntimeError):
    """No liquidity / no available pool / aggregator returned empty."""


class QuoteStaleError(RuntimeError):
    """Returned by providers whose quotes have an expiry (e.g. 0x's permit2 quotes)."""


class RouteProvider(ABC):
    name: str  # subclasses set this

    @abstractmethod
    def quote(
        self,
        w3: Web3,
        chain: ChainConfig,
        sender: str,
        token_in: TokenInfo,
        token_out: TokenInfo,
        amount_in_wei: int,
        slippage_bps: int,
    ) -> Quote:
        """Return a complete `Quote` or raise NoRouteError."""
        ...

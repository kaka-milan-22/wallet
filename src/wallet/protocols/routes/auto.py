"""Auto-fallback route provider.

Tries each underlying provider in order; returns the first successful Quote.
If all providers raise NoRouteError, re-raises a NoRouteError carrying every
provider's error message so the operator can see why each tier failed.

Typical configuration: `AutoFallbackRoute([ZeroExRoute(), UniswapV3DirectRoute()])`
— prefer aggregator pricing on mainnet, degrade to direct Uniswap V3 (more
reliable on testnets where aggregator routes can be sparse).
"""

from __future__ import annotations

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo
from wallet.protocols.routes.base import NoRouteError, Quote, RouteProvider


class AutoFallbackRoute(RouteProvider):
    name = "auto"

    def __init__(self, providers: list[RouteProvider]):
        if not providers:
            raise ValueError("AutoFallbackRoute needs at least one provider")
        self._providers = list(providers)

    def quote(
        self,
        w3,
        chain: ChainConfig,
        sender: str,
        token_in: TokenInfo,
        token_out: TokenInfo,
        amount_in_wei: int,
        slippage_bps: int,
    ) -> Quote:
        errors: list[str] = []
        for provider in self._providers:
            try:
                return provider.quote(
                    w3=w3, chain=chain, sender=sender,
                    token_in=token_in, token_out=token_out,
                    amount_in_wei=amount_in_wei, slippage_bps=slippage_bps,
                )
            except NoRouteError as e:
                errors.append(f"{provider.name}: {e}")
                continue

        raise NoRouteError(
            "all route providers failed:\n  - " + "\n  - ".join(errors)
        )

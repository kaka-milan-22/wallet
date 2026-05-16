"""0x v2 Swap API route provider (allowance-holder mode).

Uses the legacy "approve a router contract" flow (no Permit2 / EIP-712).
Endpoint: https://api.0x.org/swap/allowance-holder/quote
Auth: `0x-api-key` header + `0x-version: v2` header.

API key (free tier from dashboard.0x.org) is read from the `WALLET_ZEROX_API_KEY`
env var. When unset, this route raises NoRouteError so AutoFallbackRoute can
degrade to UniswapV3 cleanly.

Native ETH is signalled by the sentinel `0xEeeeeEeee...EEeE` per 0x convention;
the on-chain calldata still references the chain's WETH where applicable.
"""

from __future__ import annotations

import os

import httpx
from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo
from wallet.protocols.routes.base import NoRouteError, Quote, RouteProvider

# 0x v2 routes ERC-20 approvals through a single AllowanceHolder contract per
# chain. We pin the expected spender at chain-config time so a compromised
# api.0x.org response (or DNS-level MITM that survives our TLS handshake)
# can't redirect approvals to an attacker-controlled router that the user
# happens to have allowlisted for some other protocol. See
# `docs/security_review.md` F7.
_ZEROX_PROTOCOL_KEY = "zerox"
_ZEROX_ALLOWANCE_HOLDER_FIELD = "allowance_holder"

ZEROX_QUOTE_URL = "https://api.0x.org/swap/allowance-holder/quote"
ZEROX_NATIVE_SENTINEL = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
ZEROX_TIMEOUT_SECONDS = 15


class ZeroExRoute(RouteProvider):
    name = "0x"

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
        api_key = os.environ.get("WALLET_ZEROX_API_KEY")
        if not api_key:
            raise NoRouteError(
                "0x: WALLET_ZEROX_API_KEY env var is not set "
                "(get a free key at https://dashboard.0x.org)"
            )

        # Route on is_native, not symbol — symbol() on a 0x… contract is
        # attacker-controlled. See security_review.md Vuln 1.
        is_native_in = token_in.is_native
        is_native_out = token_out.is_native
        sell_token = ZEROX_NATIVE_SENTINEL if is_native_in else token_in.address
        buy_token = ZEROX_NATIVE_SENTINEL if is_native_out else token_out.address

        params = {
            "chainId": chain.chain_id,
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmount": str(amount_in_wei),
            "taker": Web3.to_checksum_address(sender),
            "slippageBps": slippage_bps,
        }
        headers = {"0x-api-key": api_key, "0x-version": "v2"}

        try:
            r = httpx.get(
                ZEROX_QUOTE_URL, params=params, headers=headers,
                timeout=ZEROX_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise NoRouteError(f"0x API request failed: {type(e).__name__}: {e}")

        if r.status_code == 404 or r.status_code == 422:
            raise NoRouteError(
                f"0x: no route for {token_in.symbol} → {token_out.symbol} "
                f"(HTTP {r.status_code}: {r.text[:200]})"
            )
        if r.status_code != 200:
            raise NoRouteError(
                f"0x API returned HTTP {r.status_code}: {r.text[:200]}"
            )

        try:
            data = r.json()
        except ValueError as e:
            raise NoRouteError(f"0x returned non-JSON response: {e}")

        if "transaction" not in data:
            raise NoRouteError(
                f"0x response missing `transaction` field: {str(data)[:200]}"
            )

        tx_part = data["transaction"]
        try:
            buy_amount = int(data["buyAmount"])
            min_buy = int(data.get("minBuyAmount", data["buyAmount"]))
            to_addr = Web3.to_checksum_address(tx_part["to"])
            calldata = tx_part["data"]
            tx_value = int(tx_part.get("value", 0))
        except (KeyError, ValueError, TypeError) as e:
            raise NoRouteError(f"0x response shape unexpected: {e}; body={str(data)[:300]}")

        # Spender for ERC-20 approve. 0x reports it in `issues.allowance.spender`
        # when an approval is required (or absent if already approved / native in).
        issues = data.get("issues") or {}
        allowance_issue = issues.get("allowance") or {}
        spender_raw = allowance_issue.get("spender") or to_addr
        spender = Web3.to_checksum_address(spender_raw)

        # Pin both `to_addr` and `spender` to the chain-configured AllowanceHolder.
        # In 0x v2 allowance-holder mode every legitimate quote routes through
        # the same AllowanceHolder for both fields. A compromised api.0x.org
        # could otherwise return:
        #   - `spender` = a router the user already approved for another
        #     protocol (e.g. UniswapV3Router from a stale approval) so the
        #     wallet's allowance pre-check passes silently; OR
        #   - `to_addr` = that same router with malicious calldata, draining
        #     funds via the existing approval the user forgot to revoke.
        # Pinning both catches either variant. Fail-closed: missing chain
        # config = no 0x route on this chain.
        pinned_raw = chain.protocols.get(_ZEROX_PROTOCOL_KEY, {}).get(
            _ZEROX_ALLOWANCE_HOLDER_FIELD
        )
        if not pinned_raw:
            raise NoRouteError(
                f"0x: chain {chain.name!r} has no `zerox.allowance_holder` "
                f"configured. Add the AllowanceHolder address to chains.json "
                f"(or the built-in preset) before routing through 0x."
            )
        pinned = Web3.to_checksum_address(pinned_raw)
        if to_addr.lower() != pinned.lower():
            raise NoRouteError(
                f"0x: quote routes tx to {to_addr}, expected chain-pinned "
                f"AllowanceHolder {pinned}. Refusing to sign — api.0x.org "
                f"may be compromised, the AllowanceHolder migrated, or the "
                f"chain config is out of date."
            )
        if spender.lower() != pinned.lower():
            raise NoRouteError(
                f"0x: quote returned unexpected spender {spender}; expected "
                f"chain-pinned AllowanceHolder {pinned}. Refusing to sign — "
                f"this protects against quotes that claim approval is needed "
                f"for a contract the user already trusts for other protocols."
            )

        # Human-readable route summary from the 0x response's route.fills
        route_parts: list[str] = []
        route = data.get("route") or {}
        fills = route.get("fills") or []
        for f in fills:
            src = f.get("source") or "?"
            prop = f.get("proportionBps")
            if prop and prop != "10000":
                route_parts.append(f"{src}({int(prop) / 100:.1f}%)")
            else:
                route_parts.append(src)
        if not route_parts:
            route_parts = ["0x"]
        route_description = f"0x: {', '.join(route_parts)}"

        return Quote(
            route_provider=self.name,
            route_description=route_description,
            to=to_addr,
            data=calldata,
            value=tx_value,
            token_in_address=token_in.address,
            token_out_address=token_out.address,
            token_in_symbol=token_in.symbol,
            token_out_symbol=token_out.symbol,
            token_in_decimals=token_in.decimals,
            token_out_decimals=token_out.decimals,
            amount_in_wei=amount_in_wei,
            amount_out_expected_wei=buy_amount,
            amount_out_min_wei=min_buy,
            spender=spender,
        )

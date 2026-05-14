"""Thin client for Etherscan v2 unified API.

The unified endpoint is `https://api.etherscan.io/v2/api` with `chainid=...`
selecting the network. An API key (free tier) is required and read from
`ETHERSCAN_API_KEY`.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from wallet.core.config import ChainConfig


class EtherscanError(RuntimeError):
    pass


def _api_key() -> str:
    k = os.getenv("ETHERSCAN_API_KEY")
    if not k:
        raise EtherscanError(
            "ETHERSCAN_API_KEY env var is required. Get one free at "
            "https://etherscan.io/myapikey"
        )
    return k


def _call(chain: ChainConfig, params: dict[str, Any]) -> Any:
    full = {**params, "chainid": chain.chain_id, "apikey": _api_key()}
    try:
        r = httpx.get(chain.explorer_api_url, params=full, timeout=20)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        # `str(e)` and `e.request.url` both contain `apikey=…` as a query
        # parameter. Surface only the status — never the URL — so the
        # apikey doesn't end up in `rpc_error.reason` envelopes the agent
        # reads.
        raise EtherscanError(
            f"etherscan returned HTTP {e.response.status_code}"
        ) from None
    except httpx.RequestError as e:
        # Same risk: `httpx.RequestError.__str__` may include the URL.
        # `e.request.url` is also available; we deliberately discard both.
        raise EtherscanError(
            f"etherscan request failed: {type(e).__name__}"
        ) from None
    data = r.json()

    status = data.get("status")
    msg = data.get("message", "")
    result = data.get("result")

    if status == "1":
        return result
    # Etherscan signals "no rows" with status="0" and message="No transactions found"
    if isinstance(msg, str) and ("No transactions found" in msg or "No records found" in msg):
        return []
    raise EtherscanError(f"etherscan API error: {msg or status}: {result}")


def list_native_txs(chain: ChainConfig, address: str, limit: int = 25) -> list[dict]:
    """Return latest native + contract-call transactions for `address`."""
    return _call(
        chain,
        {
            "module": "account",
            "action": "txlist",
            "address": address,
            "page": 1,
            "offset": limit,
            "sort": "desc",
        },
    )


def list_token_txs(chain: ChainConfig, address: str, limit: int = 25) -> list[dict]:
    """Return latest ERC-20 transfers in/out of `address`."""
    return _call(
        chain,
        {
            "module": "account",
            "action": "tokentx",
            "address": address,
            "page": 1,
            "offset": limit,
            "sort": "desc",
        },
    )

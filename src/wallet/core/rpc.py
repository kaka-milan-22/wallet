from __future__ import annotations

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, Timeout
from web3 import HTTPProvider, Web3
from web3.exceptions import Web3RPCError

from wallet.core.config import ChainConfig


class RpcConnectError(RuntimeError):
    """Raised when the RPC endpoint is unreachable, unauthenticated,
    rate-limited, or returns a fatal JSON-RPC error during the chainId
    handshake. CLI commands convert this to an `rpc_error` envelope so
    the user gets a clean message instead of a Python traceback."""


def make_web3(chain: ChainConfig, timeout: int = 20) -> Web3:
    """Build a Web3 client for the given chain config.

    Verifies that the configured RPC actually serves the expected chainId.
    Wraps any HTTP / network / JSON-RPC error during the handshake in
    `RpcConnectError` so callers don't need to know about requests/web3
    internals.
    """
    w3 = Web3(HTTPProvider(chain.rpc_url, request_kwargs={"timeout": timeout}))
    try:
        actual = w3.eth.chain_id
    except HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        raise RpcConnectError(
            f"RPC {chain.rpc_url} returned HTTP {status}: "
            f"{str(e.response.text)[:200] if e.response is not None else e}"
        ) from e
    except (RequestsConnectionError, Timeout) as e:
        raise RpcConnectError(
            f"failed to reach RPC {chain.rpc_url}: {type(e).__name__}: {e}"
        ) from e
    except Web3RPCError as e:
        raise RpcConnectError(
            f"RPC {chain.rpc_url} rejected chainId query: {e}"
        ) from e

    if actual != chain.chain_id:
        raise RpcConnectError(
            f"RPC chainId mismatch: config says {chain.chain_id} ({chain.name}), "
            f"endpoint reports {actual}. Likely wrong rpc_url for this chain."
        )
    return w3


def format_units(amount: int, decimals: int) -> str:
    """Render a raw integer (wei / smallest token unit) as a fixed-point string."""
    if decimals == 0:
        return str(amount)
    sign = "-" if amount < 0 else ""
    n = abs(amount)
    s = str(n).rjust(decimals + 1, "0")
    integer = s[:-decimals]
    fraction = s[-decimals:].rstrip("0")
    if not fraction:
        return f"{sign}{integer}"
    return f"{sign}{integer}.{fraction}"


def parse_units(value: str, decimals: int) -> int:
    """Inverse of `format_units` — parse a decimal string into raw integer units."""
    s = value.strip()
    if not s:
        raise ValueError("empty amount")
    sign = 1
    if s.startswith("-"):
        sign = -1
        s = s[1:]
    if "." in s:
        integer, fraction = s.split(".", 1)
    else:
        integer, fraction = s, ""
    if len(fraction) > decimals:
        raise ValueError(
            f"amount has {len(fraction)} fractional digits but token only allows {decimals}"
        )
    fraction = fraction.ljust(decimals, "0")
    integer = integer or "0"
    if not (integer.isdigit() and (fraction == "" or fraction.isdigit())):
        raise ValueError(f"invalid amount: {value!r}")
    return sign * (int(integer) * (10**decimals) + (int(fraction) if fraction else 0))

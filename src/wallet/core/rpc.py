from __future__ import annotations

from web3 import HTTPProvider, Web3

from wallet.core.config import ChainConfig


def make_web3(chain: ChainConfig, timeout: int = 20) -> Web3:
    """Build a Web3 client for the given chain config.

    Verifies that the configured RPC actually serves the expected chainId.
    """
    w3 = Web3(HTTPProvider(chain.rpc_url, request_kwargs={"timeout": timeout}))
    actual = w3.eth.chain_id
    if actual != chain.chain_id:
        raise RuntimeError(
            f"RPC chainId mismatch: config says {chain.chain_id} ({chain.name}), "
            f"endpoint reports {actual}"
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

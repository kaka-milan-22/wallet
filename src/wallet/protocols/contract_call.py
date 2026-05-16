"""Generic contract call — the typed-policy escape hatch.

Lets the user call any function on any contract by passing a human-readable
function signature plus positional args. Goes through the same simulate +
EIP-1559 + idempotency pipeline as typed prepare_* helpers, but the
description carries `kind = "contract call <fn>"` so policy classifies it
as the `contract_call` category — which is hard-blocked for agent callers
and floor-gated by `contract_allowlist` + `sentinel_blocklist` + value caps
for TTY callers.

Intentionally minimal: no token resolution, no allowance pre-check, no
slippage helpers, no fancy preview. The whole point is to be a thin wrapper
that doesn't grow per-protocol code. Callers signing this take responsibility
for understanding the calldata.
"""

from __future__ import annotations

import json
import re
from typing import Any

from eth_abi import encode as eth_abi_encode
from eth_utils import function_signature_to_4byte_selector
from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.core.tx import PreparedTx, _common_fields, _simulate, _strip_nonce


__all__ = [
    "ArgCoercionError",
    "SignatureParseError",
    "build_calldata",
    "coerce_arg",
    "parse_function_signature",
    "prepare_contract_call",
]


class SignatureParseError(ValueError):
    """Function signature didn't parse cleanly."""


class ArgCoercionError(ValueError):
    """A CLI-provided arg couldn't be coerced to the declared ABI type."""

    def __init__(self, *, arg_index: int, abi_type: str, value: str, reason: str):
        self.arg_index = arg_index
        self.abi_type = abi_type
        self.value = value
        self.reason = reason
        super().__init__(
            f"arg #{arg_index} (type={abi_type}, value={value!r}): {reason}"
        )


# Matches `name(type1,type2,...)` with optional whitespace. Tuples (parenthesised
# inner type lists) are explicitly rejected at the type-validation step below —
# v1 supports simple types and 1D arrays only, which covers ~all real-world
# contract calls a user types by hand. Typed prepare_* already exists for the
# rest (UniV3 mint params, etc.).
_SIG_RE = re.compile(r"^\s*([a-zA-Z_]\w*)\s*\((.*)\)\s*$", re.DOTALL)

# Simple type or 1D array thereof. `[]` or `[N]` suffix permitted.
_TYPE_RE = re.compile(
    r"^("
    r"address|bool|string|bytes"
    r"|u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)"
    r"|bytes(?:[1-9]|[12][0-9]|3[0-2])"
    r")(\[\d*\])?$"
)


def parse_function_signature(sig: str) -> tuple[str, list[str]]:
    """Return (function_name, [abi_types]) from a Solidity-style signature.

    Examples:
        "transfer(address,uint256)"            → ("transfer", ["address","uint256"])
        "setApprovalForAll(address,bool)"      → ("setApprovalForAll", ["address","bool"])
        "name()"                                → ("name", [])
        "claim(uint256[])"                      → ("claim", ["uint256[]"])

    Tuple types (parenthesised inner lists) are NOT supported in v1. The
    typed prepare_* helpers cover the protocols where tuples matter (UniV3
    mint, etc.).
    """
    m = _SIG_RE.match(sig)
    if not m:
        raise SignatureParseError(
            f"signature must match `name(types,...)`; got {sig!r}"
        )
    name, raw_types = m.group(1), m.group(2).strip()
    if not raw_types:
        return name, []

    if "(" in raw_types or ")" in raw_types:
        raise SignatureParseError(
            "tuple parameter types are not supported in v1 — use a typed "
            "command (e.g. `wallet lp mint`) for protocols that take tuples"
        )

    types = [t.strip() for t in raw_types.split(",")]
    for t in types:
        if not _TYPE_RE.match(t):
            raise SignatureParseError(
                f"unsupported / unknown ABI type {t!r}. v1 supports "
                f"address / bool / string / uintN / intN / bytes / bytesN "
                f"and 1D arrays of these."
            )
    return name, types


def _coerce_scalar(value: str, abi_type: str, arg_index: int) -> Any:
    """Coerce a single CLI string to the Python type eth_abi.encode expects.

    Disambiguates int/hex by auto-detecting `0x` prefix on numeric types.
    """
    if abi_type == "address":
        v = value.strip()
        if not (v.startswith("0x") and len(v) == 42):
            raise ArgCoercionError(
                arg_index=arg_index, abi_type=abi_type, value=value,
                reason="address must be 0x-prefixed 20-byte hex",
            )
        # eth_abi accepts checksum-cased addresses verbatim.
        return Web3.to_checksum_address(v)

    if abi_type == "bool":
        s = value.strip().lower()
        if s in ("true", "1"):
            return True
        if s in ("false", "0"):
            return False
        raise ArgCoercionError(
            arg_index=arg_index, abi_type=abi_type, value=value,
            reason="bool must be true/false/1/0",
        )

    if abi_type == "string":
        return value

    if abi_type == "bytes" or abi_type.startswith("bytes"):
        s = value.strip()
        if not s.startswith("0x"):
            raise ArgCoercionError(
                arg_index=arg_index, abi_type=abi_type, value=value,
                reason="bytes must be 0x-prefixed hex",
            )
        try:
            b = bytes.fromhex(s[2:])
        except ValueError as e:
            raise ArgCoercionError(
                arg_index=arg_index, abi_type=abi_type, value=value,
                reason=f"invalid hex: {e}",
            ) from None
        # Fixed-size byteN: validate length matches the type
        if abi_type != "bytes":
            n = int(abi_type[len("bytes"):])
            if len(b) != n:
                raise ArgCoercionError(
                    arg_index=arg_index, abi_type=abi_type, value=value,
                    reason=f"expected {n} bytes, got {len(b)}",
                )
        return b

    if abi_type.startswith("uint") or abi_type.startswith("int"):
        s = value.strip()
        try:
            # `int(s, 0)` auto-detects 0x / 0o / decimal; rejects junk.
            return int(s, 0)
        except ValueError:
            raise ArgCoercionError(
                arg_index=arg_index, abi_type=abi_type, value=value,
                reason="not a valid integer (decimal or 0x-hex)",
            ) from None

    raise ArgCoercionError(
        arg_index=arg_index, abi_type=abi_type, value=value,
        reason="internal: scalar coercer hit unreachable type",
    )


def coerce_arg(value: str, abi_type: str, arg_index: int = 0) -> Any:
    """Coerce one CLI string arg to its declared ABI type (incl. 1D arrays).

    Arrays expect a JSON list (`[1,2,3]` or `["0xabc...","0xdef..."]`).
    """
    if abi_type.endswith("]"):
        # 1D array of a simple type
        bracket = abi_type.rfind("[")
        elem_type = abi_type[:bracket]
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as e:
            raise ArgCoercionError(
                arg_index=arg_index, abi_type=abi_type, value=value,
                reason=f"array must be JSON (e.g. [1,2,3]); {e}",
            ) from None
        if not isinstance(raw, list):
            raise ArgCoercionError(
                arg_index=arg_index, abi_type=abi_type, value=value,
                reason="array arg must be a JSON list",
            )
        return [_coerce_scalar(str(x), elem_type, arg_index) for x in raw]
    return _coerce_scalar(value, abi_type, arg_index)


def build_calldata(fn_sig: str, args: list[str]) -> tuple[str, str, list[str], list[Any]]:
    """Parse + coerce + ABI-encode in one shot.

    Returns (calldata_hex, fn_name, abi_types, typed_args). The trailing
    components are returned alongside the calldata so the CLI can show a
    decoded preview without re-parsing the signature.
    """
    fn_name, types = parse_function_signature(fn_sig)
    if len(args) != len(types):
        raise ArgCoercionError(
            arg_index=-1, abi_type="(arity)", value=str(args),
            reason=f"function {fn_name} takes {len(types)} args, got {len(args)}",
        )
    typed = [coerce_arg(a, t, i) for i, (a, t) in enumerate(zip(args, types))]
    selector = function_signature_to_4byte_selector(
        f"{fn_name}({','.join(types)})"
    )
    encoded = eth_abi_encode(types, typed) if types else b""
    return "0x" + selector.hex() + encoded.hex(), fn_name, types, typed


def _decoded_args_preview(types: list[str], typed: list[Any]) -> list[dict[str, Any]]:
    """Shape `args` for the preview / JSON envelope — addresses checksummed,
    bytes shown as 0x-hex, ints stringified to avoid JSON precision loss."""
    out: list[dict[str, Any]] = []
    for t, v in zip(types, typed):
        if isinstance(v, bytes):
            display: Any = "0x" + v.hex()
        elif isinstance(v, int):
            display = str(v)
        elif isinstance(v, list):
            display = [
                ("0x" + x.hex()) if isinstance(x, bytes)
                else str(x) if isinstance(x, int)
                else x
                for x in v
            ]
        else:
            display = v
        out.append({"type": t, "value": display})
    return out


def prepare_contract_call(
    w3: Web3,
    chain: ChainConfig,
    sender: str,
    to: str,
    fn_sig: str,
    args: list[str],
    value_wei: int = 0,
) -> PreparedTx:
    """Build an unsigned generic contract call.

    Mirrors the existing prepare_* shape: pre-simulates via eth_call, fills
    EIP-1559 fees, leaves nonce blank (signed-time refresh in
    confirm_and_broadcast). Description carries `kind = "contract call <fn>"`
    so policy routes it through the `contract_call` category.
    """
    calldata, fn_name, types, typed_args = build_calldata(fn_sig, args)

    to_cs = Web3.to_checksum_address(to)
    base = _common_fields(w3, chain, sender)
    tx: dict[str, Any] = {
        **base,
        "to": to_cs,
        "value": int(value_wei),
        "data": calldata,
    }
    tx["gas"] = w3.eth.estimate_gas(tx)
    _simulate(w3, tx)
    _strip_nonce(tx)
    fee_wei = tx["maxFeePerGas"] * tx["gas"]

    canonical_sig = f"{fn_name}({','.join(types)})"
    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee_wei,
        description={
            # Kind starts with "contract call" so policy._category routes here.
            "kind": f"contract call {fn_name}",
            "from": tx["from"],
            "to": to_cs,
            # `value` is the native asset moved by this tx (msg.value). We
            # surface it under amount_wei so the existing per-tx native cap
            # automatically applies — generic calls don't get a free pass
            # around `max_per_tx: {ETH: ...}`.
            "amount_wei": int(value_wei),
            "amount_unit": chain.native_symbol,
            "amount_decimals": 18,
            "cc_function_signature": canonical_sig,
            "cc_function_name": fn_name,
            "cc_args": _decoded_args_preview(types, typed_args),
            "cc_calldata": calldata,
            "cc_value_wei": int(value_wei),
        },
    )

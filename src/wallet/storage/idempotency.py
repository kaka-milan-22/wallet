"""Idempotency store: ensures retried `wallet send` / `wallet approve`
operations don't broadcast twice when the agent retries on transient errors.

Storage: `~/.wallet/idempotency.json` (single JSON object, key=request_id).
TTL: 24h by default; expired entries are swept on each `record()`.

Stripe-style semantics: same request_id with same fingerprint → return cached
result; same request_id with DIFFERENT fingerprint → raise IdempotencyMismatch
(this is a programming error — the agent reused an ID for a different op).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wallet.core.config import atomic_write_text, data_root
from pydantic import BaseModel

__all__ = [
    "CachedResult",
    "DEFAULT_TTL_HOURS",
    "IdempotencyMismatch",
    "fingerprint",
    "lookup",
    "record",
    "store_path",
    "sweep_expired",
]

DEFAULT_TTL_HOURS = 24


class IdempotencyMismatch(RuntimeError):
    """A request_id was reused with different parameters."""


class CachedResult(BaseModel):
    request_id: str
    fingerprint: str
    tx_hash: str | None
    nonce: int | None
    outcome: str  # "broadcast" for now; future: also "policy_blocked", etc.
    detail: str = ""
    created_at: str
    expires_at: str


def store_path() -> Path:
    return data_root() / "idempotency.json"


def _load() -> dict[str, dict]:
    p = store_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, dict]) -> None:
    atomic_write_text(store_path(), json.dumps(data, indent=2, sort_keys=True))


def _norm_addr(v):
    """Lowercase a 0x… address; pass anything else (None, ints) through unchanged.

    Checksum casing must not affect the fingerprint, or two ostensibly identical
    operations will hash differently depending on where the address was sourced.
    """
    if isinstance(v, str) and v.startswith("0x"):
        return v.lower()
    return v


def fingerprint(prepared, chain) -> str:
    """Stable hash of the operation parameters. Same logical op → same hash.

    Security-critical: a too-narrow fingerprint causes silent replay of the
    WRONG cached tx_hash when an agent reuses a request_id across logically
    different ops. See security_review.md Vuln 2. The contract is that any
    description field that influences on-chain effects must contribute here.

    Addresses are lowercased so checksum-casing differences (e.g. EIP-55 vs
    lower-hex sources) cannot produce divergent fingerprints for the same op.

    Nonce is intentionally NOT included: a retry with a fresh nonce (because a
    previous attempt advanced chain state) is still the same logical op.
    """
    desc = prepared.description
    canonical = json.dumps(
        {
            # Chain identity: include chain_id, not just the human name, so a
            # forked / re-aliased chain config can't shadow a different chain.
            "chain": chain.name,
            "chain_id": int(getattr(chain, "chain_id", 0) or 0),
            "kind": desc.get("kind"),
            "from": _norm_addr(desc.get("from")),
            "to": _norm_addr(desc.get("to")),
            "spender": _norm_addr(desc.get("spender")),
            "amount_wei": str(desc.get("amount_wei", 0)),
            "unit": desc.get("amount_unit"),
            # Transfer / approve token (kind="<SYM> transfer" / "<SYM> approve")
            "token": _norm_addr(desc.get("token_address")),
            # Swap-specific fields. Without these, two swaps that differ only in
            # output token or min-out collapse to the same hash and the second
            # replays the FIRST's tx_hash silently.
            "swap_token_in": _norm_addr(desc.get("swap_token_in_address")),
            "swap_token_out": _norm_addr(desc.get("swap_token_out_address")),
            "swap_amount_out_min_wei": str(desc.get("swap_amount_out_min_wei", "")),
            # Aave-specific fields. Asset address + action distinguishes
            # supply USDC vs supply WETH at the same pool.
            "aave_action": desc.get("aave_action"),
            "aave_asset": _norm_addr(desc.get("aave_asset_address")),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sweep_expired(data: dict[str, dict] | None = None) -> dict[str, dict]:
    """Remove entries whose expires_at is in the past. Returns the cleaned dict."""
    if data is None:
        data = _load()
    now = _now_utc()
    fresh = {}
    for k, v in data.items():
        try:
            exp = datetime.fromisoformat(v.get("expires_at", ""))
        except ValueError:
            continue  # malformed — drop
        if exp > now:
            fresh[k] = v
    if len(fresh) != len(data):
        _save(fresh)
    return fresh


def lookup(request_id: str, fingerprint_hash: str) -> CachedResult | None:
    """Return cached result for `request_id`, or None if not seen / expired.

    Raises IdempotencyMismatch if `request_id` was previously used with
    different parameters.
    """
    data = sweep_expired()
    raw = data.get(request_id)
    if raw is None:
        return None

    cached = CachedResult(**raw)
    if cached.fingerprint != fingerprint_hash:
        raise IdempotencyMismatch(
            f"request_id '{request_id}' was previously used for a different "
            f"operation. Generate a fresh request-id (e.g. uuidgen) for new ops."
        )
    return cached


def record(
    request_id: str,
    fingerprint_hash: str,
    *,
    tx_hash: str | None,
    nonce: int | None,
    outcome: str,
    detail: str = "",
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> None:
    now = _now_utc()
    expires = now + timedelta(hours=ttl_hours)

    data = sweep_expired()
    data[request_id] = CachedResult(
        request_id=request_id,
        fingerprint=fingerprint_hash,
        tx_hash=tx_hash,
        nonce=nonce,
        outcome=outcome,
        detail=detail,
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    ).model_dump()
    _save(data)

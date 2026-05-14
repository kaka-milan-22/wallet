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


def fingerprint(prepared, chain) -> str:
    """Stable hash of the operation parameters. Same logical op → same hash."""
    desc = prepared.description
    canonical = json.dumps(
        {
            "chain": chain.name,
            "from": desc.get("from"),
            "to": desc.get("to"),
            "spender": desc.get("spender"),
            "kind": desc.get("kind"),
            "amount_wei": str(desc.get("amount_wei", 0)),
            "unit": desc.get("amount_unit"),
            "token": desc.get("token_address"),
            # Note: nonce is NOT in the fingerprint. A retry of the same logical
            # request with a fresh nonce (because previous attempt advanced
            # chain state) is still the same logical op.
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

"""Stuck-tx recovery: list pending broadcasts, build cancel/replacement txs.

EIP-1559 mempool replacement rule: a new tx with same (`from`, `nonce`) replaces
the prior one if BOTH `maxFeePerGas` and `maxPriorityFeePerGas` are ≥ old × 110%.
We compute `max(old × 1.1, base*2 + bumped_priority)` so the new tx clears both
the mempool replacement threshold AND current chain pricing.

- `prepare_cancel` builds a 0-value self-send → replacing the original makes the
  nonce "spent" on a no-op, so the original never lands.
- `prepare_replacement` re-fetches the stuck tx's calldata via the RPC and
  rebuilds an identical payload at the same nonce with bumped fees → the
  original operation lands faster.

Both produce a `PreparedTx` that flows through the existing
`confirm_and_broadcast` pipeline (policy / idempotency / audit). The PreparedTx
keeps its `nonce` field set (unlike fresh prepares which defer nonce to
sign-time), so the broadcast helper has to be told `preserve_nonce=True` here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from web3 import Web3
from web3.exceptions import TransactionNotFound

from wallet.core.config import ChainConfig
from wallet.core.tx import PreparedTx, _fees
from wallet.storage import idempotency

__all__ = [
    "PendingTx",
    "StuckTxError",
    "list_pending",
    "prepare_cancel",
    "prepare_replacement",
]


# EIP-1559 mempool replacement: new gas must be ≥ old × 110%.
_MIN_BUMP_BPS = 1100  # 1.10×


class StuckTxError(RuntimeError):
    """Raised when a cancel/replace cannot be constructed (mined / no-cache)."""


@dataclass
class PendingTx:
    """A broadcast recorded in idempotency.json whose receipt hasn't landed yet."""

    request_id: str
    tx_hash: str
    nonce: int
    from_address: str
    created_at: str
    kind: str
    description: dict | None


def list_pending(w3: Web3, account_address: str) -> list[PendingTx]:
    """Return broadcasts in idempotency.json for `account_address` that have
    no receipt on chain yet (still in mempool or never propagated).

    Filters out: receipts present + blockNumber set (mined), non-broadcast
    outcomes (rejected / replayed_idempotent / superseded), other accounts,
    and entries whose nonce has been consumed on chain by a different tx (a
    successful cancel/replace pushes the account nonce past the cached one,
    leaving the displaced original with no receipt and no chance of mining).
    """
    addr = account_address.lower()
    out: list[PendingTx] = []
    data = idempotency.sweep_expired()

    # One `eth_getTransactionCount` for `latest` is cheap and lets us filter
    # superseded entries (cached nonce < on-chain nonce ⇒ that slot has been
    # consumed — either by this tx mining or by a replacement). Without it,
    # the displaced original sits in the cache forever showing as "pending".
    chain_nonce: int | None = None
    try:
        chain_nonce = int(w3.eth.get_transaction_count(
            Web3.to_checksum_address(account_address), "latest"
        ))
    except Exception:
        # If RPC fails we fall back to the receipt-only filter — pending
        # may include stale entries but we don't drop real ones.
        chain_nonce = None

    for req_id, raw in data.items():
        cached = idempotency.CachedResult(**raw)
        if cached.outcome != "broadcast":
            continue
        if cached.tx_hash is None or cached.nonce is None:
            continue
        if cached.from_address is None or cached.from_address.lower() != addr:
            continue
        if chain_nonce is not None and int(cached.nonce) < chain_nonce:
            # Nonce slot already consumed on chain by some tx — either this
            # one (handled by the receipt check below) or a replacement.
            # In either case it's not pending.
            try:
                receipt = w3.eth.get_transaction_receipt(cached.tx_hash)
                if receipt is not None and getattr(receipt, "blockNumber", None) is not None:
                    continue  # cached tx itself mined
            except TransactionNotFound:
                continue  # cached tx was displaced by a replacement
            except Exception:
                continue  # don't surface a "pending" we know is dead
        try:
            receipt = w3.eth.get_transaction_receipt(cached.tx_hash)
            if receipt is not None and getattr(receipt, "blockNumber", None) is not None:
                continue  # mined
        except TransactionNotFound:
            pass
        except Exception:
            # Defensive: any RPC hiccup → treat as still pending and surface it.
            pass
        desc = cached.description or {}
        out.append(PendingTx(
            request_id=req_id,
            tx_hash=cached.tx_hash,
            nonce=int(cached.nonce),
            from_address=cached.from_address,
            created_at=cached.created_at,
            kind=desc.get("kind", "unknown"),
            description=desc,
        ))
    out.sort(key=lambda p: p.nonce)
    return out


def _bumped_fees(
    w3: Web3,
    *,
    old_max_fee: int | None,
    old_priority: int | None,
    speedup_pct: int,
) -> tuple[int, int]:
    """Compute (priority, max_fee) bumped per EIP-1559 replacement rule.

    Each return value satisfies BOTH:
      - ≥ old × max(1.10, 1 + speedup_pct/100)   (mempool replacement floor)
      - ≥ current chain pricing (base*2 + priority floor)
    """
    chain_priority, chain_max_fee = _fees(w3)

    bump_bps = max(_MIN_BUMP_BPS, 10_000 + speedup_pct * 100)

    if old_priority is not None:
        priority_bumped = (int(old_priority) * bump_bps) // 10_000
    else:
        priority_bumped = chain_priority
    if old_max_fee is not None:
        max_fee_bumped = (int(old_max_fee) * bump_bps) // 10_000
    else:
        max_fee_bumped = chain_max_fee

    priority = max(priority_bumped, chain_priority)
    max_fee = max(max_fee_bumped, chain_max_fee, priority)
    return priority, max_fee


def _find_cached_by_nonce(account_address: str, nonce: int) -> idempotency.CachedResult | None:
    addr = account_address.lower()
    data = idempotency.sweep_expired()
    for raw in data.values():
        cached = idempotency.CachedResult(**raw)
        if cached.outcome != "broadcast":
            continue
        if cached.nonce != nonce:
            continue
        if cached.from_address and cached.from_address.lower() == addr:
            return cached
    return None


def prepare_cancel(
    w3: Web3,
    chain: ChainConfig,
    account_address: str,
    nonce: int,
    speedup_pct: int = 25,
) -> PreparedTx:
    """0-value self-send at `nonce` with gas bumped enough to replace.

    Looks up any prior tx at this nonce in the idempotency cache to read old
    gas (if available) and clears EIP-1559 mempool replacement floor + current
    chain pricing.
    """
    cached = _find_cached_by_nonce(account_address, nonce)
    old_max_fee = None
    old_priority = None
    old_tx_hash = None
    if cached is not None:
        old_tx_hash = cached.tx_hash
        # The cached description has gas only if the original prepared tx
        # carried it; fresh prepares defer nonce/gas, but we stored the
        # description AFTER signing-time refresh so signed gas is in there.
        d = cached.description or {}
        # Older entries may not have gas fields — that's fine, _bumped_fees
        # falls back to chain pricing.
        old_max_fee = d.get("max_fee_per_gas")
        old_priority = d.get("max_priority_fee_per_gas")

    priority, max_fee = _bumped_fees(
        w3,
        old_max_fee=old_max_fee,
        old_priority=old_priority,
        speedup_pct=speedup_pct,
    )

    addr_cs = Web3.to_checksum_address(account_address)
    tx: dict[str, Any] = {
        "from": addr_cs,
        "to": addr_cs,
        "value": 0,
        "data": "0x",
        "gas": 21000,
        "nonce": int(nonce),
        "chainId": chain.chain_id,
        "type": 2,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority,
    }
    description: dict[str, Any] = {
        "kind": "tx cancel",
        "is_self_send_for_cancel": True,
        "cancel_nonce": int(nonce),
        "from": addr_cs,
        "to": addr_cs,
        "amount_wei": 0,
        "amount_unit": chain.native_symbol,
        "amount_decimals": 18,
        "max_fee_per_gas": max_fee,
        "max_priority_fee_per_gas": priority,
    }
    if old_tx_hash:
        description["old_tx_hash"] = old_tx_hash

    return PreparedTx(
        tx=tx,
        estimated_fee_wei=21000 * max_fee,
        description=description,
    )


def prepare_replacement(
    w3: Web3,
    chain: ChainConfig,
    account_address: str,
    nonce: int,
    speedup_pct: int = 25,
) -> PreparedTx:
    """Re-send the original tx's calldata at `nonce` with bumped gas.

    Reads the original tx via `eth.getTransaction(cached_hash)` to recover
    `to / value / input / gas`. Raises `StuckTxError` if no cached entry
    exists for this nonce or if the original tx has already been mined.
    """
    cached = _find_cached_by_nonce(account_address, nonce)
    if cached is None:
        raise StuckTxError(
            f"no cached broadcast for nonce {nonce} on account "
            f"{account_address} — replace can only operate on txs originally "
            f"broadcast through this wallet"
        )
    if cached.tx_hash is None:
        raise StuckTxError(f"cached entry for nonce {nonce} has no tx_hash")

    try:
        raw = w3.eth.get_transaction(cached.tx_hash)
    except TransactionNotFound:
        raise StuckTxError(
            f"original tx {cached.tx_hash} no longer in mempool — may have "
            f"been replaced externally or dropped"
        ) from None

    if getattr(raw, "blockNumber", None) is not None:
        raise StuckTxError(
            f"original tx {cached.tx_hash} already mined at block "
            f"{raw.blockNumber} — nothing to replace"
        )

    priority, max_fee = _bumped_fees(
        w3,
        old_max_fee=getattr(raw, "maxFeePerGas", None),
        old_priority=getattr(raw, "maxPriorityFeePerGas", None),
        speedup_pct=speedup_pct,
    )

    addr_cs = Web3.to_checksum_address(account_address)

    # `raw.input` may be bytes (HexBytes) or hex string depending on web3.py
    # version; normalize to a 0x-prefixed hex string the signer accepts.
    data = raw.input
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = "0x" + bytes(data).hex()
    elif isinstance(data, str) and not data.startswith("0x"):
        data = "0x" + data

    to_addr = raw.to
    if to_addr is not None:
        to_addr = Web3.to_checksum_address(to_addr)

    tx: dict[str, Any] = {
        "from": addr_cs,
        "to": to_addr,
        "value": int(raw.value or 0),
        "data": data,
        "gas": int(raw.gas),
        "nonce": int(nonce),
        "chainId": chain.chain_id,
        "type": 2,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority,
    }

    original_kind = (cached.description or {}).get("kind", "unknown")
    description: dict[str, Any] = {
        "kind": "tx replace",
        "is_replacement": True,
        "replace_nonce": int(nonce),
        "original_tx_hash": cached.tx_hash,
        "original_kind": original_kind,
        "from": addr_cs,
        "to": to_addr,
        "amount_wei": int(raw.value or 0),
        "amount_unit": chain.native_symbol,
        "amount_decimals": 18,
        "max_fee_per_gas": max_fee,
        "max_priority_fee_per_gas": priority,
    }
    # Forward fields from the original description that policy may consult
    # (token_address, spender, aave_*, swap_*, lp_*). Without these the policy
    # for replacement would degrade to checking only to/value/from.
    if cached.description:
        for k in (
            "token_address", "spender",
            "aave_action", "aave_asset_address", "aave_estimated_hf_after",
            "swap_token_in_address", "swap_token_out_address", "swap_amount_out_min_wei",
            "lp_action", "lp_token0_address", "lp_token1_address", "lp_fee",
            "lp_tick_lower", "lp_tick_upper", "lp_liquidity_wei",
            "lp_amount0_desired_wei", "lp_amount1_desired_wei",
            "lp_amount0_min_wei", "lp_amount1_min_wei", "lp_recipient",
            "lp_nft_token_id", "cc_calldata",
        ):
            if k in cached.description:
                description[k] = cached.description[k]

    return PreparedTx(
        tx=tx,
        estimated_fee_wei=int(raw.gas) * max_fee,
        description=description,
    )

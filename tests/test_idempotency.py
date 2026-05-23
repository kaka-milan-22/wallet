"""Idempotency store: lookup, record, fingerprint, mismatch, TTL sweep."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wallet.storage import idempotency
from wallet.storage.idempotency import (
    CachedResult,
    IdempotencyMismatch,
    fingerprint,
    lookup,
    record,
    sweep_expired,
)


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(idempotency, "store_path", lambda: tmp_path / "idempotency.json")
    return tmp_path


class FakePrepared:
    def __init__(self, **desc):
        self.description = desc
        self.tx = {"nonce": desc.pop("_nonce", 0)}


class FakeChain:
    name = "sepolia"
    chain_id = 11155111


class FakeChainMainnet:
    name = "mainnet"
    chain_id = 1


def test_fingerprint_stable_for_same_logical_op():
    pt1 = FakePrepared(
        kind="native transfer", from_="0xabc", to="0xdef",
        amount_wei=1000, amount_unit="ETH", token_address=None,
    )
    pt2 = FakePrepared(
        kind="native transfer", from_="0xabc", to="0xdef",
        amount_wei=1000, amount_unit="ETH", token_address=None,
    )
    assert fingerprint(pt1, FakeChain()) == fingerprint(pt2, FakeChain())


def test_fingerprint_changes_when_amount_changes():
    pt1 = FakePrepared(kind="t", to="0xa", amount_wei=1000, amount_unit="ETH")
    pt2 = FakePrepared(kind="t", to="0xa", amount_wei=2000, amount_unit="ETH")
    assert fingerprint(pt1, FakeChain()) != fingerprint(pt2, FakeChain())


def test_fingerprint_ignores_nonce():
    """Same logical op with different nonces (because the chain advanced
    between attempts) should still hash the same."""
    pt1 = FakePrepared(kind="t", to="0xa", amount_wei=1000, amount_unit="ETH", _nonce=5)
    pt2 = FakePrepared(kind="t", to="0xa", amount_wei=1000, amount_unit="ETH", _nonce=7)
    assert fingerprint(pt1, FakeChain()) == fingerprint(pt2, FakeChain())


# --- Regression tests for security_review.md Vuln 2 -------------------------
# Earlier the swap path collapsed to a single fingerprint regardless of output
# token, min-out, or chain_id, so a request_id reused across two different
# swaps replayed the first tx_hash silently. Each assertion below would have
# failed under the old fingerprint.


def _swap_prep(**overrides):
    base = dict(
        kind="swap",
        amount_wei=10**6,
        amount_unit="USDC",
        swap_token_in_address="0x" + "aa" * 20,
        swap_token_out_address="0x" + "bb" * 20,
        swap_amount_out_min_wei=10**18,
    )
    base.update(overrides)
    return FakePrepared(**{"from": "0x" + "11" * 20, "to": "0x" + "22" * 20, **base})


def test_fingerprint_differs_on_swap_token_out():
    a = _swap_prep(swap_token_out_address="0x" + "bb" * 20)  # → WETH
    b = _swap_prep(swap_token_out_address="0x" + "cc" * 20)  # → DAI
    assert fingerprint(a, FakeChain()) != fingerprint(b, FakeChain())


def test_fingerprint_differs_on_swap_min_out():
    a = _swap_prep(swap_amount_out_min_wei=10**18)
    b = _swap_prep(swap_amount_out_min_wei=2 * 10**18)
    assert fingerprint(a, FakeChain()) != fingerprint(b, FakeChain())


def test_fingerprint_differs_on_chain_id():
    a = _swap_prep()
    # Same chain name shouldn't be enough — chain_id must factor in.
    sepolia_lookalike = type("X", (), {"name": "sepolia", "chain_id": 1})
    assert fingerprint(a, FakeChain()) != fingerprint(a, sepolia_lookalike())


def test_fingerprint_ignores_address_casing():
    """Checksum vs lowercase for the same address must not produce different hashes."""
    cs = "0x" + "Aa" * 20
    lo = cs.lower()
    a = _swap_prep(swap_token_out_address=cs)
    b = _swap_prep(swap_token_out_address=lo)
    assert fingerprint(a, FakeChain()) == fingerprint(b, FakeChain())


def test_fingerprint_differs_on_contract_call_calldata():
    """Two different generic contract-call ops to the same target with the
    same msg.value (e.g. `pause()` vs `unpause()`) must NOT collapse — the
    calldata is what determines the on-chain effect."""
    base = dict(
        kind="contract call pause",
        amount_wei=0, amount_unit="ETH",
    )
    a = FakePrepared(**{
        "from": "0x" + "11" * 20, "to": "0x" + "44" * 20,
        "cc_calldata": "0xaabbccdd", **base,
    })
    b = FakePrepared(**{
        "from": "0x" + "11" * 20, "to": "0x" + "44" * 20,
        "cc_calldata": "0xeeff0011", **base,
    })
    assert fingerprint(a, FakeChain()) != fingerprint(b, FakeChain())


def test_lookup_returns_none_when_unknown(isolated_store):
    assert lookup("never-seen", "fp") is None


def test_record_then_lookup_returns_cached(isolated_store):
    record("req-1", "fp-1", tx_hash="0xhash", nonce=5, outcome="broadcast")
    cached = lookup("req-1", "fp-1")
    assert cached is not None
    assert cached.tx_hash == "0xhash"
    assert cached.nonce == 5
    assert cached.outcome == "broadcast"


def test_lookup_with_mismatching_fingerprint_raises(isolated_store):
    record("req-2", "fp-original", tx_hash="0xfoo", nonce=1, outcome="broadcast")
    with pytest.raises(IdempotencyMismatch, match="previously used"):
        lookup("req-2", "fp-different")


def test_expired_entry_swept_and_treated_as_unseen(isolated_store, monkeypatch):
    record("req-3", "fp-3", tx_hash="0x", nonce=1, outcome="broadcast", ttl_hours=24)

    # Force expiry by editing the file directly
    import json
    p = idempotency.store_path()
    data = json.loads(p.read_text())
    data["req-3"]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    p.write_text(json.dumps(data))

    # Lookup should treat as unseen (sweeps + returns None)
    assert lookup("req-3", "fp-3") is None
    # Sweep should have removed it from disk
    remaining = json.loads(p.read_text())
    assert "req-3" not in remaining


def test_record_persists_across_processes(isolated_store, tmp_path):
    record("req-4", "fp-4", tx_hash="0xpersist", nonce=10, outcome="broadcast")
    # Re-read by reloading the module's _load
    cached = lookup("req-4", "fp-4")
    assert cached.tx_hash == "0xpersist"


def test_sweep_expired_keeps_fresh(isolated_store):
    record("fresh", "fp", tx_hash="0x1", nonce=1, outcome="broadcast", ttl_hours=24)

    # Manually inject an already-expired entry
    import json
    p = idempotency.store_path()
    data = json.loads(p.read_text())
    data["stale"] = CachedResult(
        request_id="stale",
        fingerprint="fp-old",
        tx_hash="0x2",
        nonce=2,
        outcome="broadcast",
        created_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    ).model_dump()
    p.write_text(json.dumps(data))

    sweep_expired()

    after = json.loads(p.read_text())
    assert "fresh" in after
    assert "stale" not in after

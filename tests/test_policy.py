"""Policy schema + decision tree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wallet.core import policy as policy_mod
from wallet.core.policy import Decision, Policy, evaluate, save_policy
from wallet.core.tokens import MAX_UINT256
from wallet.storage import audit
from wallet.storage import state as state_mod
from wallet.storage.state import (
    AccountEntry,
    TokenEntry,
    WalletState,
    WatchEntry,
)


@pytest.fixture
def isolated_files(tmp_path: Path, monkeypatch):
    """Redirect policy.json + audit.log + state.json into a per-test dir."""
    monkeypatch.setattr(policy_mod, "policy_path", lambda: tmp_path / "policy.json")
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "audit.log")
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "state.json")
    return tmp_path


class FakePrepared:
    def __init__(self, **desc):
        self.description = desc


def _state_with(book=None, accounts=None, watch=None) -> WalletState:
    return WalletState(
        accounts=accounts or [],
        book=book or {},
        watch=watch or [],
        tokens=[],
    )


# --- bypass + missing policy ------------------------------------------------


def test_no_policy_blocks_agent(isolated_files):
    pt = FakePrepared(kind="native transfer", to="0x" + "11" * 20, amount_wei=1, amount_unit="ETH", amount_decimals=18)
    d = evaluate(pt, _state_with(), "agent")
    assert not d.allowed
    assert "no-policy-configured" in d.reason


def test_no_policy_blocks_tty_too(isolated_files):
    pt = FakePrepared(kind="native transfer", to="0x" + "11" * 20, amount_wei=1, amount_unit="ETH", amount_decimals=18)
    d = evaluate(pt, _state_with(), "tty")
    assert not d.allowed


def test_bypass_in_agent_mode_is_blocked(isolated_files):
    save_policy(Policy(max_per_tx={"ETH": "0.001"}, recipient_allowlist=["0x" + "22" * 20]))
    pt = FakePrepared(kind="native transfer", to="0x" + "11" * 20, amount_wei=10**18, amount_unit="ETH", amount_decimals=18)
    d = evaluate(pt, _state_with(), "agent", bypass=True)
    assert not d.allowed
    assert "bypass:not-allowed-in-agent-mode" == d.reason


def test_bypass_in_tty_mode_allows_with_warn(isolated_files):
    save_policy(Policy(max_per_tx={"ETH": "0.001"}))
    pt = FakePrepared(kind="native transfer", to="0x" + "11" * 20, amount_wei=10**18, amount_unit="ETH", amount_decimals=18)
    d = evaluate(pt, _state_with(), "tty", bypass=True)
    assert d.allowed
    assert d.severity == "warn"


# --- sentinel ---------------------------------------------------------------


def test_sentinel_blocks_even_when_in_allowlist(isolated_files):
    addr = "0x" + "ee" * 20
    save_policy(Policy(
        recipient_allowlist=[addr],
        sentinel_blocklist=[addr],
        max_per_tx={"ETH": "1"},
    ))
    pt = FakePrepared(kind="native transfer", to=addr, amount_wei=1, amount_unit="ETH", amount_decimals=18)
    d = evaluate(pt, _state_with(), "tty")
    assert not d.allowed
    assert d.reason == "sentinel-blocklisted"


# --- approve checks ----------------------------------------------------------


def test_unlimited_approve_blocked_by_default(isolated_files):
    spender = "0x" + "33" * 20
    save_policy(Policy(contract_allowlist=[spender]))
    pt = FakePrepared(
        kind="USDC approve", spender=spender,
        amount_wei=MAX_UINT256, amount_unit="USDC", amount_decimals=6,
    )
    d = evaluate(pt, _state_with(), "agent")
    assert not d.allowed
    assert d.reason == "unlimited-approve-denied"


def test_approve_to_non_allowlisted_spender_blocked(isolated_files):
    save_policy(Policy(contract_allowlist=["0x" + "44" * 20]))
    pt = FakePrepared(
        kind="USDC approve", spender="0x" + "55" * 20,
        amount_wei=10**6, amount_unit="USDC", amount_decimals=6,
    )
    d = evaluate(pt, _state_with(), "agent")
    assert not d.allowed
    assert d.reason == "spender-not-in-contract-allowlist"


def test_approve_within_limits_allowed(isolated_files):
    spender = "0x" + "44" * 20
    save_policy(Policy(
        contract_allowlist=[spender],
        max_per_tx={"USDC": "1000"},
    ))
    pt = FakePrepared(
        kind="USDC approve", spender=spender,
        amount_wei=100 * 10**6, amount_unit="USDC", amount_decimals=6,
    )
    d = evaluate(pt, _state_with(), "agent")
    assert d.allowed


# --- send recipient allowlist ------------------------------------------------


def test_send_to_non_allowlisted_recipient_blocked(isolated_files):
    save_policy(Policy(
        recipient_allowlist=["0x" + "11" * 20],
        max_per_tx={"ETH": "1"},
    ))
    pt = FakePrepared(
        kind="native transfer", to="0x" + "22" * 20,
        amount_wei=10**15, amount_unit="ETH", amount_decimals=18,
    )
    d = evaluate(pt, _state_with(), "agent")
    assert not d.allowed
    assert d.reason == "recipient-not-in-allowlist"


def test_send_to_allowlisted_address(isolated_files):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        recipient_allowlist=[addr],
        max_per_tx={"ETH": "1"},
        first_send_warn=False,  # already known via allowlist for this test
    ))
    pt = FakePrepared(
        kind="native transfer", to=addr,
        amount_wei=10**15, amount_unit="ETH", amount_decimals=18,
    )
    state = _state_with(book={"alice": addr})
    d = evaluate(pt, state, "agent")
    assert d.allowed


def test_send_to_alias_resolves_via_book(isolated_files):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        recipient_allowlist=["@alice"],
        max_per_tx={"ETH": "1"},
        first_send_warn=False,
    ))
    pt = FakePrepared(
        kind="native transfer", to=addr,
        amount_wei=10**15, amount_unit="ETH", amount_decimals=18,
    )
    state = _state_with(book={"alice": addr})
    d = evaluate(pt, state, "agent")
    assert d.allowed


# --- per-tx caps -------------------------------------------------------------


def test_per_tx_cap_exceeded(isolated_files):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        recipient_allowlist=[addr],
        max_per_tx={"ETH": "0.001"},
        first_send_warn=False,
    ))
    pt = FakePrepared(
        kind="native transfer", to=addr,
        amount_wei=10**18,  # 1 ETH
        amount_unit="ETH", amount_decimals=18,
    )
    d = evaluate(pt, _state_with(book={"alice": addr}), "agent")
    assert not d.allowed
    assert "max-per-tx-exceeded:ETH:0.001" in d.reason


# --- per-day cap (reads audit log) -------------------------------------------


def test_per_day_cap_uses_audit_log(isolated_files):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        recipient_allowlist=[addr],
        max_per_tx={"ETH": "0.1"},
        max_per_day={"ETH": "0.05"},
        first_send_warn=False,
    ))
    # Pre-seed audit log with 0.04 ETH already sent today
    audit.write({
        "outcome": "broadcast",
        "unit": "ETH",
        "amount_wei": str(int(0.04 * 10**18)),
        "to": "0x" + "99" * 20,
    })
    # Try to send another 0.02 — total would be 0.06, exceeds 0.05 cap
    pt = FakePrepared(
        kind="native transfer", to=addr,
        amount_wei=int(0.02 * 10**18),
        amount_unit="ETH", amount_decimals=18,
    )
    d = evaluate(pt, _state_with(book={"alice": addr}), "agent")
    assert not d.allowed
    assert "max-per-day-exceeded" in d.reason


# --- first-send semantics ----------------------------------------------------


def test_first_send_blocks_agent(isolated_files):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        recipient_allowlist=[addr],
        max_per_tx={"ETH": "1"},
        first_send_warn=True,
    ))
    pt = FakePrepared(
        kind="native transfer", to=addr,
        amount_wei=10**15, amount_unit="ETH", amount_decimals=18,
    )
    # state knows addr (via allowlist resolution) but not via book/watch/accounts/audit
    d = evaluate(pt, _state_with(), "agent")
    assert not d.allowed
    assert d.reason == "first-send-blocked-for-agent"


def test_first_send_warns_tty(isolated_files):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        recipient_allowlist=[addr],
        max_per_tx={"ETH": "1"},
        first_send_warn=True,
    ))
    pt = FakePrepared(
        kind="native transfer", to=addr,
        amount_wei=10**15, amount_unit="ETH", amount_decimals=18,
    )
    d = evaluate(pt, _state_with(), "tty")
    assert d.allowed
    assert d.severity == "warn"
    assert d.reason == "first-send-warn"


def test_known_recipient_via_book_no_first_send_warn(isolated_files):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        recipient_allowlist=[addr],
        max_per_tx={"ETH": "1"},
        first_send_warn=True,
    ))
    pt = FakePrepared(
        kind="native transfer", to=addr,
        amount_wei=10**15, amount_unit="ETH", amount_decimals=18,
    )
    d = evaluate(pt, _state_with(book={"alice": addr}), "agent")
    assert d.allowed
    assert d.severity == "allow"


# --- swap category -----------------------------------------------------------


def test_swap_blocked_when_router_not_in_contract_allowlist(isolated_files):
    router = "0x" + "33" * 20
    save_policy(Policy(
        max_per_tx={"USDC": "1000"},
        contract_allowlist=[],  # router NOT here
    ))
    pt = FakePrepared(
        kind="swap", to=router,
        amount_wei=10**6, amount_unit="USDC", amount_decimals=6,
    )
    d = evaluate(pt, _state_with(), "agent")
    assert not d.allowed
    assert d.reason == "swap-router-not-in-contract-allowlist"


def test_swap_allowed_when_router_in_contract_allowlist(isolated_files):
    router = "0x" + "33" * 20
    save_policy(Policy(
        max_per_tx={"USDC": "1000"},
        contract_allowlist=[router],
    ))
    pt = FakePrepared(
        kind="swap", to=router,
        amount_wei=10**6, amount_unit="USDC", amount_decimals=6,
    )
    d = evaluate(pt, _state_with(), "agent")
    assert d.allowed


def test_swap_per_tx_cap_applies_to_token_in(isolated_files):
    router = "0x" + "33" * 20
    save_policy(Policy(
        max_per_tx={"USDC": "10"},  # cap of 10 USDC
        contract_allowlist=[router],
    ))
    pt = FakePrepared(
        kind="swap", to=router,
        amount_wei=100 * 10**6,  # 100 USDC, way over cap
        amount_unit="USDC", amount_decimals=6,
    )
    d = evaluate(pt, _state_with(), "agent")
    assert not d.allowed
    assert "max-per-tx-exceeded:USDC:10" in d.reason


# --- schema validation -------------------------------------------------------


def test_save_load_roundtrip(isolated_files):
    p = Policy(
        max_per_tx={"ETH": "0.005", "USDC": "100"},
        max_per_day={"ETH": "0.05"},
        recipient_allowlist=["0x" + "11" * 20, "@alice"],
        contract_allowlist=["0x" + "22" * 20],
        deny_unlimited_approve=True,
        first_send_warn=True,
        sentinel_blocklist=["0x" + "ee" * 20],
    )
    save_policy(p)
    from wallet.core.policy import load_policy
    loaded = load_policy()
    assert loaded == p

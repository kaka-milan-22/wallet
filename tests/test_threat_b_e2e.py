"""End-to-end interlock between caller / policy / idempotency / audit
in the `confirm_and_broadcast` pipeline.

Mocks at the signing / RPC boundary so the test runs offline; everything
above (policy gate ordering, idempotency lookup/record, audit entry timing)
is exercised against the real production code.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer

from wallet.cli import _common
from wallet.cli._common import confirm_and_broadcast
from wallet.core import policy as policy_mod
from wallet.core.policy import Policy, save_policy
from wallet.core.tx import PreparedTx
from wallet.storage import audit, idempotency
from wallet.storage.state import WalletState


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch):
    """Redirect policy / audit / idempotency files into a per-test dir."""
    monkeypatch.setattr(policy_mod, "policy_path", lambda: tmp_path / "policy.json")
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "audit.log")
    monkeypatch.setattr(idempotency, "store_path", lambda: tmp_path / "idempotency.json")
    return tmp_path


@pytest.fixture
def force_caller_agent(monkeypatch):
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "agent")


@pytest.fixture
def force_caller_tty(monkeypatch):
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "tty")


@pytest.fixture
def mock_signing(monkeypatch):
    """Replace sign + broadcast so the test never touches RPC or vault."""
    monkeypatch.setattr(_common, "sign_transaction", lambda *a, **kw: b"\x00" * 100)

    counter = {"n": 0}

    def fake_broadcast(_w3, _raw):
        counter["n"] += 1
        return f"0x{'a' * 6}{counter['n']:058x}"

    monkeypatch.setattr(_common, "broadcast", fake_broadcast)
    return counter


def _chain():
    chain = MagicMock()
    chain.name = "sepolia"
    chain.chain_id = 11155111
    chain.explorer_tx_url = "https://sepolia.etherscan.io/tx/{tx}"
    chain.native_symbol = "ETH"
    return chain


def _prepared(amount_wei: int, to: str = "0x" + "11" * 20):
    pt = PreparedTx(
        tx={"from": "0x" + "ff" * 20, "to": to, "value": amount_wei,
            "nonce": 5, "gas": 21000,
            "maxFeePerGas": 2 * 10**9, "maxPriorityFeePerGas": 10**9,
            "chainId": 11155111, "type": 2},
        estimated_fee_wei=21000 * 2 * 10**9,
        description={
            "kind": "native transfer",
            "from": "0x" + "ff" * 20,
            "to": to,
            "amount_wei": amount_wei,
            "amount_unit": "ETH",
            "amount_decimals": 18,
        },
    )
    return pt


def _read_audit(tmp_path: Path) -> list[dict]:
    p = tmp_path / "audit.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


# --- 1. Agent without policy → blocked at policy gate ------------------------


def test_agent_without_policy_is_rejected(
    isolated_dirs, force_caller_agent, mock_signing, monkeypatch
):
    pt = _prepared(amount_wei=10**15)
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)

    with pytest.raises(typer.Exit) as exc:
        confirm_and_broadcast(
            w3=MagicMock(), state=WalletState(), chain=_chain(),
            sender_account=MagicMock(), prepared=pt,
            dry_run=False, yes=True,
        )
    assert exc.value.exit_code == 3

    events = _read_audit(isolated_dirs)
    assert len(events) == 1
    assert events[0]["outcome"] == "rejected"
    assert "no-policy-configured" in events[0]["policy_decision"]
    assert mock_signing["n"] == 0  # no broadcast happened


# --- 2. Agent with policy but no request_id → blocked ------------------------


def test_agent_with_policy_without_request_id_rejected(
    isolated_dirs, force_caller_agent, mock_signing
):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        max_per_tx={"ETH": "0.1"},
        recipient_allowlist=[addr],
        first_send_warn=False,
    ))
    pt = _prepared(amount_wei=10**15, to=addr)

    with pytest.raises(typer.Exit) as exc:
        confirm_and_broadcast(
            w3=MagicMock(), state=WalletState(), chain=_chain(),
            sender_account=MagicMock(), prepared=pt,
            dry_run=False, yes=True,
        )
    assert exc.value.exit_code == 3

    events = _read_audit(isolated_dirs)
    assert events[-1]["outcome"] == "rejected"
    assert "missing-request-id-for-agent" in events[-1]["policy_decision"]
    assert mock_signing["n"] == 0


# --- 3. Full path: agent + policy + request_id → broadcast -------------------


def test_agent_full_compliant_broadcasts(
    isolated_dirs, force_caller_agent, mock_signing
):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        max_per_tx={"ETH": "0.1"},
        recipient_allowlist=[addr],
        first_send_warn=False,
    ))
    pt = _prepared(amount_wei=10**15, to=addr)

    confirm_and_broadcast(
        w3=MagicMock(), state=WalletState(), chain=_chain(),
        sender_account=MagicMock(), prepared=pt,
        dry_run=False, yes=True, request_id="req-aaa-001",
    )

    assert mock_signing["n"] == 1  # broadcast called once
    events = _read_audit(isolated_dirs)
    assert events[-1]["outcome"] == "broadcast"
    assert events[-1]["request_id"] == "req-aaa-001"


# --- 4. Idempotent replay: same request_id → cached, no second broadcast -----


def test_idempotent_replay(
    isolated_dirs, force_caller_agent, mock_signing
):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        max_per_tx={"ETH": "0.1"},
        recipient_allowlist=[addr],
        first_send_warn=False,
    ))
    pt = _prepared(amount_wei=10**15, to=addr)

    confirm_and_broadcast(
        w3=MagicMock(), state=WalletState(), chain=_chain(),
        sender_account=MagicMock(), prepared=pt,
        dry_run=False, yes=True, request_id="req-replay-001",
    )
    first_count = mock_signing["n"]

    # Second call with same request_id, same params — must replay, not re-sign
    confirm_and_broadcast(
        w3=MagicMock(), state=WalletState(), chain=_chain(),
        sender_account=MagicMock(), prepared=pt,
        dry_run=False, yes=True, request_id="req-replay-001",
    )

    assert mock_signing["n"] == first_count, "broadcast should not have been called again"
    events = _read_audit(isolated_dirs)
    assert events[-1]["outcome"] == "replayed_idempotent"
    assert events[-1]["request_id"] == "req-replay-001"


# --- 5. Cap exceeded → blocked, audit recorded, no broadcast -----------------


def test_per_tx_cap_blocks_with_audit(
    isolated_dirs, force_caller_agent, mock_signing
):
    addr = "0x" + "11" * 20
    save_policy(Policy(
        max_per_tx={"ETH": "0.0001"},
        recipient_allowlist=[addr],
        first_send_warn=False,
    ))
    # Try to send 1 ETH — way above the 0.0001 cap
    pt = _prepared(amount_wei=10**18, to=addr)

    with pytest.raises(typer.Exit) as exc:
        confirm_and_broadcast(
            w3=MagicMock(), state=WalletState(), chain=_chain(),
            sender_account=MagicMock(), prepared=pt,
            dry_run=False, yes=True, request_id="req-overcap-001",
        )
    assert exc.value.exit_code == 3
    assert mock_signing["n"] == 0

    events = _read_audit(isolated_dirs)
    assert events[-1]["outcome"] == "rejected"
    assert "max-per-tx-exceeded" in events[-1]["policy_decision"]
    # request_id is recorded even on rejection (so reuse can be detected)
    assert events[-1]["request_id"] == "req-overcap-001"


# --- 6. TTY bypass works, agent bypass refused -------------------------------


def test_tty_bypass_allows(isolated_dirs, force_caller_tty, mock_signing):
    save_policy(Policy(max_per_tx={"ETH": "0.0001"}))
    pt = _prepared(amount_wei=10**18)  # over cap

    confirm_and_broadcast(
        w3=MagicMock(), state=WalletState(), chain=_chain(),
        sender_account=MagicMock(), prepared=pt,
        dry_run=False, yes=True, policy_bypass=True,
    )
    assert mock_signing["n"] == 1
    events = _read_audit(isolated_dirs)
    assert events[-1]["outcome"] == "broadcast"
    assert "bypass:tty" in events[-1]["policy_decision"]


def test_agent_bypass_refused(isolated_dirs, force_caller_agent, mock_signing):
    save_policy(Policy(max_per_tx={"ETH": "1"}))
    pt = _prepared(amount_wei=10**15)

    with pytest.raises(typer.Exit):
        confirm_and_broadcast(
            w3=MagicMock(), state=WalletState(), chain=_chain(),
            sender_account=MagicMock(), prepared=pt,
            dry_run=False, yes=True, policy_bypass=True,
            request_id="req-bypass-attempt",
        )
    assert mock_signing["n"] == 0
    events = _read_audit(isolated_dirs)
    assert "bypass:not-allowed-in-agent-mode" in events[-1]["policy_decision"]

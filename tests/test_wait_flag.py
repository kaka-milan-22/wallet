"""--wait flag: post-broadcast receipt polling and envelope shape.

Covers the three terminal outcomes of `--wait` (success, reverted, timeout)
plus the idempotent-replay path (cache hit polls the cached tx_hash too).
The broadcast itself is always mocked — these tests are about what
`confirm_and_broadcast` does with `w3.eth.wait_for_transaction_receipt`,
not about real RPC behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer
from web3 import Web3
from web3.exceptions import TimeExhausted

from wallet.cli import _common
from wallet.cli._common import _poll_receipt, confirm_and_broadcast
from wallet.core import policy as policy_mod
from wallet.core.config import ChainConfig
from wallet.core.policy import Policy, save_policy
from wallet.core.tx import PreparedTx
from wallet.storage import audit, idempotency
from wallet.storage import state as state_mod
from wallet.storage.state import AccountEntry, WalletState


SEPOLIA = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="http://read.invalid",
    broadcast_rpc_url=None,
    mev_exposure=False,
    explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}",
    native_symbol="ETH",
)


@pytest.fixture
def isolated_files(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(policy_mod, "policy_path", lambda: tmp_path / "policy.json")
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "audit.log")
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(idempotency, "store_path", lambda: tmp_path / "idempotency.json")
    return tmp_path


def _state_one_account() -> WalletState:
    return WalletState(
        accounts=[AccountEntry(
            name="main",
            address="0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
            default=True,
            derivation_path="m/44'/60'/0'/0/0",
            vault_key="wallet/main/mnemonic",
        )],
        book={},
        watch=[],
        tokens=[],
    )


def _self_send_prepared() -> PreparedTx:
    addr = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
    return PreparedTx(
        tx={
            "from": addr,
            "to": addr,
            "value": 1,
            "chainId": 11155111,
            "type": 2,
            "maxFeePerGas": 10**9,
            "maxPriorityFeePerGas": 10**9,
            "gas": 21000,
        },
        estimated_fee_wei=10**9 * 21000,
        description={
            "kind": "native transfer",
            "from": addr,
            "to": addr,
            "amount_wei": 1,
            "amount_unit": "ETH",
            "amount_decimals": 18,
        },
    )


def _stub_w3():
    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    w3.eth.get_transaction_count = MagicMock(return_value=42)
    return w3


def _enable_json(monkeypatch):
    monkeypatch.setenv("WALLET_JSON", "1")
    from wallet.cli._output import OutputMode
    monkeypatch.setattr(OutputMode, "json", True)


def _allow_policy():
    save_policy(Policy(
        recipient_allowlist=["0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"],
        max_per_tx={"ETH": "1"},
        first_send_warn=False,
    ))


# --- _poll_receipt unit tests ----------------------------------------------


def test_poll_receipt_success_populates_block_and_fee_fields():
    """status=1 receipt → status="success" plus block_number / gas_used /
    effective_fee_wei = gas_used * effective_gas_price."""
    w3 = MagicMock()
    receipt = {
        "status": 1,
        "blockNumber": 12345,
        "blockHash": MagicMock(hex=lambda: "0xabc123"),
        "gasUsed": 21000,
        "effectiveGasPrice": 2 * 10**9,
        "transactionIndex": 7,
    }
    w3.eth.wait_for_transaction_receipt.return_value = receipt

    out = _poll_receipt(w3, "0xdead", timeout=30)

    assert out["status"] == "success"
    assert out["block_number"] == 12345
    assert out["block_hash"] == "0xabc123"
    assert out["gas_used"] == 21000
    assert out["effective_gas_price_wei"] == str(2 * 10**9)
    assert out["effective_fee_wei"] == str(21000 * 2 * 10**9)
    # 21000 * 2e9 wei = 4.2e13 wei = 0.000042 ETH
    assert out["effective_fee"] == "0.000042"
    assert out["tx_index"] == 7


def test_poll_receipt_reverted_status_zero():
    w3 = MagicMock()
    w3.eth.wait_for_transaction_receipt.return_value = {
        "status": 0,
        "blockNumber": 99,
        "blockHash": MagicMock(hex=lambda: "0xbeef"),
        "gasUsed": 50000,
        "effectiveGasPrice": 10**9,
    }
    out = _poll_receipt(w3, "0xdead", timeout=30)
    assert out["status"] == "reverted"
    assert out["gas_used"] == 50000


def test_poll_receipt_timeout_carries_tx_hash_for_recovery():
    """On TimeExhausted the broadcast tx_hash must round-trip out so the
    caller / user can re-query later. We do NOT raise — the broadcast
    itself succeeded; only the receipt is unknown."""
    w3 = MagicMock()
    w3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted("timeout")
    out = _poll_receipt(w3, "0xfeed", timeout=15)
    assert out == {"status": "timeout", "waited_seconds": 15, "tx_hash": "0xfeed"}


# --- confirm_and_broadcast + --wait end-to-end -----------------------------


def _setup_mock_broadcast(monkeypatch, tx_hash="0xdeadbeef"):
    monkeypatch.setattr(_common, "web3_broadcast", lambda chain: MagicMock())
    monkeypatch.setattr(_common, "sign_transaction", lambda *a, **kw: b"\x01")
    monkeypatch.setattr(_common, "broadcast", lambda *a, **kw: tx_hash)


def test_wait_false_omits_wait_key_from_envelope(isolated_files, monkeypatch, capsys):
    """Default (wait=False) must not poll for receipt and must not add a
    `wait` field — backwards-compatible with all existing JSON consumers."""
    _allow_policy()
    _enable_json(monkeypatch)
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "agent")
    _setup_mock_broadcast(monkeypatch)

    w3 = _stub_w3()
    confirm_and_broadcast(
        w3, _state_one_account(), SEPOLIA, _state_one_account().accounts[0],
        _self_send_prepared(),
        dry_run=False, yes=True, request_id="no-wait-1",
    )

    # Receipt poll must NOT have been called at all.
    assert not w3.eth.wait_for_transaction_receipt.called

    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert envelope["ok"] is True
    assert "wait" not in envelope["data"]


def test_wait_success_merges_receipt_into_envelope(isolated_files, monkeypatch, capsys):
    _allow_policy()
    _enable_json(monkeypatch)
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "agent")
    _setup_mock_broadcast(monkeypatch, tx_hash="0xaaa1")

    w3 = _stub_w3()
    w3.eth.wait_for_transaction_receipt.return_value = {
        "status": 1,
        "blockNumber": 7777,
        "blockHash": MagicMock(hex=lambda: "0xblockhashhex"),
        "gasUsed": 21000,
        "effectiveGasPrice": 3 * 10**9,
    }

    confirm_and_broadcast(
        w3, _state_one_account(), SEPOLIA, _state_one_account().accounts[0],
        _self_send_prepared(),
        dry_run=False, yes=True, request_id="wait-ok-1",
        wait=True, wait_timeout=30,
    )

    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert envelope["ok"] is True
    assert envelope["data"]["wait"]["status"] == "success"
    assert envelope["data"]["wait"]["block_number"] == 7777
    assert envelope["data"]["wait"]["gas_used"] == 21000


def test_wait_reverted_flips_envelope_to_error_and_exits_5(
    isolated_files, monkeypatch, capsys
):
    """A reverted tx is an on-chain failure even though the broadcast itself
    succeeded — surface ok=false / code=tx_reverted / exit 5 so agents notice."""
    _allow_policy()
    _enable_json(monkeypatch)
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "agent")
    _setup_mock_broadcast(monkeypatch, tx_hash="0xaaa2")

    w3 = _stub_w3()
    w3.eth.wait_for_transaction_receipt.return_value = {
        "status": 0,
        "blockNumber": 8888,
        "blockHash": MagicMock(hex=lambda: "0xblock2"),
        "gasUsed": 75000,
        "effectiveGasPrice": 10**9,
    }

    with pytest.raises(typer.Exit) as exc:
        confirm_and_broadcast(
            w3, _state_one_account(), SEPOLIA, _state_one_account().accounts[0],
            _self_send_prepared(),
            dry_run=False, yes=True, request_id="wait-revert-1",
            wait=True, wait_timeout=30,
        )
    assert exc.value.exit_code == 5

    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert envelope["ok"] is False
    assert envelope["code"] == "tx_reverted"
    assert envelope["data"]["wait"]["status"] == "reverted"
    assert envelope["data"]["wait"]["block_number"] == 8888
    # tx_hash still present — the user needs it to inspect the failure
    assert envelope["data"]["tx_hash"] == "0xaaa2"


def test_wait_timeout_keeps_ok_true_so_broadcast_isnt_lost(
    isolated_files, monkeypatch, capsys
):
    """Timeout means we couldn't confirm — but the broadcast itself succeeded
    and the tx may still mine. Envelope stays ok=true so the caller still
    learns the tx_hash and can re-query."""
    _allow_policy()
    _enable_json(monkeypatch)
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "agent")
    _setup_mock_broadcast(monkeypatch, tx_hash="0xaaa3")

    w3 = _stub_w3()
    w3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted("timeout")

    confirm_and_broadcast(
        w3, _state_one_account(), SEPOLIA, _state_one_account().accounts[0],
        _self_send_prepared(),
        dry_run=False, yes=True, request_id="wait-timeout-1",
        wait=True, wait_timeout=5,
    )

    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert envelope["ok"] is True
    assert envelope["data"]["wait"]["status"] == "timeout"
    assert envelope["data"]["wait"]["waited_seconds"] == 5
    assert envelope["data"]["wait"]["tx_hash"] == "0xaaa3"
    assert envelope["data"]["tx_hash"] == "0xaaa3"


def test_wait_on_idempotent_replay_polls_cached_tx_hash(
    isolated_files, monkeypatch, capsys
):
    """Replay path with --wait must also poll for the cached tx_hash so the
    second call to the same request_id returns the same receipt info, not
    just the bare broadcast envelope from cache."""
    _allow_policy()
    _enable_json(monkeypatch)
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "agent")
    _setup_mock_broadcast(monkeypatch, tx_hash="0xrep1")

    w3 = _stub_w3()
    w3.eth.wait_for_transaction_receipt.return_value = {
        "status": 1,
        "blockNumber": 3333,
        "blockHash": MagicMock(hex=lambda: "0xb3"),
        "gasUsed": 21000,
        "effectiveGasPrice": 10**9,
    }

    # First call: live broadcast, no wait — populates idempotency cache.
    state = _state_one_account()
    confirm_and_broadcast(
        w3, state, SEPOLIA, state.accounts[0], _self_send_prepared(),
        dry_run=False, yes=True, request_id="replay-wait-1",
    )
    capsys.readouterr()  # drain

    # Second call: same request_id, now with --wait. Cache hit must still
    # call wait_for_transaction_receipt on the cached tx_hash.
    confirm_and_broadcast(
        w3, state, SEPOLIA, state.accounts[0], _self_send_prepared(),
        dry_run=False, yes=True, request_id="replay-wait-1",
        wait=True, wait_timeout=30,
    )

    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert envelope["ok"] is True
    assert envelope.get("replayed") is True
    assert envelope["data"]["wait"]["status"] == "success"
    assert envelope["data"]["wait"]["block_number"] == 3333
    w3.eth.wait_for_transaction_receipt.assert_called_with(
        "0xrep1", timeout=30, poll_latency=2
    )

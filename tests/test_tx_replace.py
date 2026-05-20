"""Stuck-tx recovery: cancel / replace / list_pending.

EIP-1559 mempool replacement uses same `from` + same `nonce` + bumped gas.
Tests verify gas math, PreparedTx shape, idempotency filtering, and the
policy integration for the new `cancel` category.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from web3 import Web3
from web3.exceptions import TransactionNotFound

from wallet.core.config import ChainConfig
from wallet.core import policy as policy_mod
from wallet.core.policy import Policy, evaluate, save_policy
from wallet.storage import audit
from wallet.storage import idempotency
from wallet.storage import state as state_mod
from wallet.storage.state import WalletState

SEPOLIA = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="http://invalid",
    explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}",
    native_symbol="ETH",
)

ACCOUNT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
OTHER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


# --- shared fixtures --------------------------------------------------------


@pytest.fixture
def isolated_files(tmp_path: Path, monkeypatch):
    """Redirect policy.json + audit.log + state.json + idempotency.json into tmp."""
    monkeypatch.setattr(policy_mod, "policy_path", lambda: tmp_path / "policy.json")
    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "audit.log")
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(idempotency, "store_path", lambda: tmp_path / "idempotency.json")
    return tmp_path


def _w3_with_gas(
    *,
    base_fee_gwei: int = 10,
    priority_gwei: int = 2,
):
    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    w3.eth.max_priority_fee = Web3.to_wei(priority_gwei, "gwei")
    w3.eth.get_block.return_value = {"baseFeePerGas": Web3.to_wei(base_fee_gwei, "gwei")}
    return w3


def _record_broadcast(
    *,
    request_id: str,
    tx_hash: str,
    nonce: int,
    from_address: str = ACCOUNT,
    description: dict | None = None,
) -> None:
    """Helper: stuff a CachedResult into idempotency.json for tests."""
    desc = description or {
        "kind": "native transfer",
        "from": from_address,
        "to": OTHER,
        "amount_wei": 10**15,
        "amount_unit": "ETH",
        "amount_decimals": 18,
    }
    fp = "0" * 64  # tests don't care about fingerprint contents
    idempotency.record(
        request_id, fp,
        tx_hash=tx_hash,
        nonce=nonce,
        outcome="broadcast",
        from_address=from_address,
        description=desc,
    )


# --- prepare_cancel ---------------------------------------------------------


def test_prepare_cancel_is_self_send_zero_value():
    from wallet.core.tx_replace import prepare_cancel

    w3 = _w3_with_gas(base_fee_gwei=10, priority_gwei=2)
    pt = prepare_cancel(w3, SEPOLIA, ACCOUNT, nonce=42, speedup_pct=25)

    assert pt.tx["from"] == ACCOUNT
    assert pt.tx["to"] == ACCOUNT, "cancel is self-send"
    assert pt.tx["value"] == 0
    assert pt.tx["data"] == "0x"
    assert pt.tx["nonce"] == 42, "cancel pins to exact nonce"
    assert pt.tx["gas"] == 21000, "plain transfer gas"
    assert pt.tx["chainId"] == 11155111
    assert pt.tx["type"] == 2


def test_prepare_cancel_bumps_gas_above_chain_floor():
    """Even without prior gas hint, replacement must clear base*2+priority."""
    from wallet.core.tx_replace import prepare_cancel

    w3 = _w3_with_gas(base_fee_gwei=10, priority_gwei=2)
    pt = prepare_cancel(w3, SEPOLIA, ACCOUNT, nonce=42, speedup_pct=25)

    # Floor: base*2 + priority — current chain pricing
    base_floor_max = 2 * Web3.to_wei(10, "gwei") + Web3.to_wei(2, "gwei")

    assert pt.tx["maxFeePerGas"] >= base_floor_max
    assert pt.tx["maxPriorityFeePerGas"] >= Web3.to_wei(2, "gwei")


def test_prepare_cancel_description_carries_self_send_flag_and_nonce():
    """Policy needs `is_self_send_for_cancel` + `cancel_nonce` to allow it."""
    from wallet.core.tx_replace import prepare_cancel

    w3 = _w3_with_gas()
    pt = prepare_cancel(w3, SEPOLIA, ACCOUNT, nonce=42)

    assert pt.description["kind"] == "tx cancel"
    assert pt.description["is_self_send_for_cancel"] is True
    assert pt.description["cancel_nonce"] == 42
    assert pt.description["from"] == ACCOUNT
    assert pt.description["to"] == ACCOUNT
    assert pt.description["amount_wei"] == 0
    assert pt.description["amount_unit"] == "ETH"


# --- list_pending -----------------------------------------------------------


def test_list_pending_includes_unmined_tx_for_account(isolated_files):
    from wallet.core.tx_replace import list_pending

    _record_broadcast(request_id="r1", tx_hash="0x" + "11" * 32, nonce=5)

    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    w3.eth.get_transaction_receipt.side_effect = TransactionNotFound("not found")

    pending = list_pending(w3, ACCOUNT)
    assert len(pending) == 1
    assert pending[0].tx_hash == "0x" + "11" * 32
    assert pending[0].nonce == 5
    assert pending[0].request_id == "r1"


def test_list_pending_excludes_mined_tx(isolated_files):
    from wallet.core.tx_replace import list_pending

    _record_broadcast(request_id="r1", tx_hash="0x" + "11" * 32, nonce=5)

    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    receipt = MagicMock()
    receipt.blockNumber = 123456
    w3.eth.get_transaction_receipt.return_value = receipt

    pending = list_pending(w3, ACCOUNT)
    assert len(pending) == 0


def test_list_pending_filters_by_account(isolated_files):
    from wallet.core.tx_replace import list_pending

    _record_broadcast(request_id="mine", tx_hash="0x" + "11" * 32, nonce=5, from_address=ACCOUNT)
    _record_broadcast(request_id="other", tx_hash="0x" + "22" * 32, nonce=7, from_address=OTHER)

    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    w3.eth.get_transaction_receipt.side_effect = TransactionNotFound("not found")

    pending = list_pending(w3, ACCOUNT)
    assert len(pending) == 1
    assert pending[0].request_id == "mine"


def test_list_pending_sorted_by_nonce(isolated_files):
    from wallet.core.tx_replace import list_pending

    _record_broadcast(request_id="r9", tx_hash="0x" + "99" * 32, nonce=9)
    _record_broadcast(request_id="r3", tx_hash="0x" + "33" * 32, nonce=3)
    _record_broadcast(request_id="r5", tx_hash="0x" + "55" * 32, nonce=5)

    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    w3.eth.get_transaction_receipt.side_effect = TransactionNotFound("not found")

    pending = list_pending(w3, ACCOUNT)
    assert [p.nonce for p in pending] == [3, 5, 9]


# --- prepare_replacement ----------------------------------------------------


def _build_raw_tx_mock(
    *,
    to: str = OTHER,
    value: int = 10**15,
    data: bytes = b"",
    gas: int = 21000,
    nonce: int = 5,
    max_fee_gwei: int = 12,
    priority_gwei: int = 2,
    block_number: int | None = None,
):
    raw = MagicMock()
    raw.to = to
    raw.value = value
    raw.input = data
    raw.gas = gas
    raw.nonce = nonce
    raw.maxFeePerGas = Web3.to_wei(max_fee_gwei, "gwei")
    raw.maxPriorityFeePerGas = Web3.to_wei(priority_gwei, "gwei")
    raw.blockNumber = block_number
    return raw


def test_prepare_replacement_fetches_original_and_rebuilds_same_calldata(isolated_files):
    from wallet.core.tx_replace import prepare_replacement

    tx_hash = "0x" + "11" * 32
    _record_broadcast(request_id="r1", tx_hash=tx_hash, nonce=5)

    w3 = _w3_with_gas(base_fee_gwei=10, priority_gwei=2)
    w3.eth.get_transaction.return_value = _build_raw_tx_mock(nonce=5)

    pt = prepare_replacement(w3, SEPOLIA, ACCOUNT, nonce=5, speedup_pct=25)

    assert pt.tx["to"] == OTHER
    assert pt.tx["value"] == 10**15
    assert pt.tx["nonce"] == 5
    assert pt.tx["gas"] == 21000
    assert pt.tx["chainId"] == 11155111
    assert pt.description["kind"] == "tx replace"
    assert pt.description["replace_nonce"] == 5
    assert pt.description["original_tx_hash"] == tx_hash


def test_prepare_replacement_raises_when_already_mined(isolated_files):
    from wallet.core.tx_replace import prepare_replacement, StuckTxError

    tx_hash = "0x" + "11" * 32
    _record_broadcast(request_id="r1", tx_hash=tx_hash, nonce=5)

    w3 = _w3_with_gas()
    w3.eth.get_transaction.return_value = _build_raw_tx_mock(block_number=123)

    with pytest.raises(StuckTxError, match="already mined"):
        prepare_replacement(w3, SEPOLIA, ACCOUNT, nonce=5)


def test_prepare_replacement_raises_when_no_cached_entry_for_nonce(isolated_files):
    from wallet.core.tx_replace import prepare_replacement, StuckTxError

    # idempotency store empty
    w3 = _w3_with_gas()

    with pytest.raises(StuckTxError, match="no cached"):
        prepare_replacement(w3, SEPOLIA, ACCOUNT, nonce=99)


def test_prepare_replacement_bumps_gas_at_least_110pct_of_original(isolated_files):
    """EIP-1559 mempool replacement requires both fees >= old × 1.1."""
    from wallet.core.tx_replace import prepare_replacement

    tx_hash = "0x" + "11" * 32
    _record_broadcast(request_id="r1", tx_hash=tx_hash, nonce=5)

    w3 = _w3_with_gas(base_fee_gwei=1, priority_gwei=1)  # very low chain pricing
    w3.eth.get_transaction.return_value = _build_raw_tx_mock(
        max_fee_gwei=100, priority_gwei=10, nonce=5,
    )

    pt = prepare_replacement(w3, SEPOLIA, ACCOUNT, nonce=5, speedup_pct=25)

    old_max = Web3.to_wei(100, "gwei")
    old_priority = Web3.to_wei(10, "gwei")
    assert pt.tx["maxFeePerGas"] >= int(old_max * 1.1)
    assert pt.tx["maxPriorityFeePerGas"] >= int(old_priority * 1.1)


# --- policy integration: cancel category -----------------------------------


def _state_with_account() -> WalletState:
    from wallet.storage.state import AccountEntry
    return WalletState(
        accounts=[AccountEntry(name="main", address=ACCOUNT, vault_key="wallet/main/mnemonic", derivation_path="m/44'/60'/0'/0/0")],
        book={},
        watch=[],
        tokens=[],
    )


class FakePrepared:
    def __init__(self, **desc):
        self.description = desc


def test_policy_allows_cancel_self_send_without_recipient_allowlist(isolated_files):
    """Cancel tx is self-send 0-value — recipient_allowlist must not gate it."""
    save_policy(Policy(
        max_per_tx={"ETH": "0.01"},
        recipient_allowlist=[],  # deliberately empty
        contract_allowlist=[],
    ))
    pt = FakePrepared(
        kind="tx cancel",
        is_self_send_for_cancel=True,
        cancel_nonce=42,
        from_=ACCOUNT,
        to=ACCOUNT,
        amount_wei=0,
        amount_unit="ETH",
        amount_decimals=18,
    )
    # FakePrepared maps **desc kwargs; "from" is a Python keyword reserved word
    # so we used `from_` above. Patch the key to "from" the way real desc dicts have it.
    pt.description["from"] = pt.description.pop("from_")

    d = evaluate(pt, _state_with_account(), "agent")
    assert d.allowed, f"cancel should be allowed: {d.reason}"


def test_policy_blocks_fake_cancel_when_not_self_send(isolated_files):
    """An attacker can't slip a non-self-send through by labeling kind='tx cancel'."""
    save_policy(Policy(
        max_per_tx={"ETH": "0.01"},
        recipient_allowlist=[],
    ))
    pt = FakePrepared(
        kind="tx cancel",
        is_self_send_for_cancel=True,    # the flag says yes ...
        cancel_nonce=42,
        to=OTHER,                         # ... but to is not from
        amount_wei=0,
        amount_unit="ETH",
        amount_decimals=18,
    )
    pt.description["from"] = ACCOUNT

    d = evaluate(pt, _state_with_account(), "agent")
    assert not d.allowed
    assert "cancel-must-be-self-send" in d.reason


def test_policy_blocks_cancel_to_sentinel_address(isolated_files):
    """Defense in depth: even cancel respects sentinel_blocklist."""
    save_policy(Policy(
        max_per_tx={"ETH": "0.01"},
        recipient_allowlist=[],
        sentinel_blocklist=[ACCOUNT],  # cancel target == self == sentinel
    ))
    pt = FakePrepared(
        kind="tx cancel",
        is_self_send_for_cancel=True,
        cancel_nonce=42,
        to=ACCOUNT,
        amount_wei=0,
        amount_unit="ETH",
        amount_decimals=18,
    )
    pt.description["from"] = ACCOUNT

    d = evaluate(pt, _state_with_account(), "agent")
    assert not d.allowed
    assert d.reason == "sentinel-blocklisted"


def test_policy_blocks_non_zero_value_cancel(isolated_files):
    """A 'cancel' that moves value isn't a cancel — block."""
    save_policy(Policy(
        max_per_tx={"ETH": "0.01"},
        recipient_allowlist=[],
    ))
    pt = FakePrepared(
        kind="tx cancel",
        is_self_send_for_cancel=True,
        cancel_nonce=42,
        to=ACCOUNT,
        amount_wei=10**15,             # non-zero !
        amount_unit="ETH",
        amount_decimals=18,
    )
    pt.description["from"] = ACCOUNT

    d = evaluate(pt, _state_with_account(), "agent")
    assert not d.allowed
    assert "cancel-must-be-zero-value" in d.reason


# --- CLI smoke (catches missing imports) ------------------------------------


def test_tx_pending_help_invokes():
    from typer.testing import CliRunner
    from wallet.cli.app import app

    result = CliRunner().invoke(app, ["tx", "pending", "--help"])
    assert result.exit_code == 0
    assert "pending" in result.output.lower()


def test_tx_cancel_function_body_runs_with_all_names_resolved(monkeypatch, tmp_path):
    """Same import-coverage class as test_send_function_body_runs_with_all_names_resolved."""
    from typer.testing import CliRunner
    from wallet.cli.app import app

    sentinel = SystemExit(99)

    def early_exit(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr("wallet.cli.tx.make_web3_or_exit", early_exit)
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))

    result = CliRunner().invoke(app, ["tx", "cancel", "42"])

    assert not isinstance(result.exception, NameError), (
        f"NameError escaped from tx.cancel body: {result.exception}"
    )
    assert result.exit_code == 99, (
        f"stub never fired — exit={result.exit_code} exc={result.exception!r}"
    )


def test_tx_replace_function_body_runs_with_all_names_resolved(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from wallet.cli.app import app

    sentinel = SystemExit(99)

    def early_exit(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr("wallet.cli.tx.make_web3_or_exit", early_exit)
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))

    result = CliRunner().invoke(app, ["tx", "replace", "42"])

    assert not isinstance(result.exception, NameError)
    assert result.exit_code == 99


def test_tx_pending_function_body_runs_with_all_names_resolved(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from wallet.cli.app import app

    sentinel = SystemExit(99)

    def early_exit(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr("wallet.cli.tx.make_web3_or_exit", early_exit)
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))

    result = CliRunner().invoke(app, ["tx", "pending"])

    assert not isinstance(result.exception, NameError)
    assert result.exit_code == 99


# --- Fix coverage: classification, superseded filter, audit enrichment ------


def test_classify_table_recognises_tx_cancel_and_tx_replace():
    """Fix 1: cli/_common._CLASSIFY_TABLE must have tx_cancel / tx_replace rows.

    Without these, audit log + JSON envelope + preview label all read
    `kind: unknown` for stuck-tx recovery ops, hiding the semantic from
    forensics and humans. Policy uses its own router so it's unaffected,
    but the CLI side surface must also distinguish them.
    """
    from wallet.cli._common import _category, _kind_machine
    from wallet.core.tx import PreparedTx

    cancel = PreparedTx(
        tx={"from": ACCOUNT, "to": ACCOUNT, "value": 0, "nonce": 7},
        estimated_fee_wei=0,
        description={"kind": "tx cancel", "is_self_send_for_cancel": True},
    )
    assert _category(cancel) == "tx_cancel"
    assert _kind_machine(cancel) == "tx_cancel"

    replace = PreparedTx(
        tx={"from": ACCOUNT, "to": OTHER, "value": 1, "nonce": 7},
        estimated_fee_wei=0,
        description={"kind": "tx replace", "original_kind": "native transfer"},
    )
    assert _category(replace) == "tx_replace"
    assert _kind_machine(replace) == "tx_replace"


def test_list_pending_filters_superseded_when_nonce_consumed_on_chain(isolated_files):
    """Fix 2: a cancel/replace pushes account nonce past the cached entry.

    The displaced original has no receipt (TransactionNotFound) and its nonce
    slot is already consumed on chain. Before the fix, list_pending kept
    showing it as pending forever; after, it filters out by comparing
    cached.nonce against `eth.getTransactionCount(account, 'latest')`.
    """
    from wallet.core.tx_replace import list_pending

    _record_broadcast(request_id="displaced", tx_hash="0x" + "aa" * 32, nonce=5)

    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    # On-chain nonce has advanced to 6 — slot 5 is consumed (by a replacement,
    # since the cached tx itself returns TransactionNotFound below).
    w3.eth.get_transaction_count.return_value = 6
    w3.eth.get_transaction_receipt.side_effect = TransactionNotFound("not found")

    pending = list_pending(w3, ACCOUNT)
    assert pending == [], "displaced original at consumed nonce must not list as pending"


def test_list_pending_keeps_entry_when_chain_nonce_rpc_fails(isolated_files):
    """Fix 2 graceful degradation: if eth_getTransactionCount errors we fall
    back to the receipt-only filter — don't drop real pending entries."""
    from wallet.core.tx_replace import list_pending

    _record_broadcast(request_id="r1", tx_hash="0x" + "bb" * 32, nonce=5)

    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    w3.eth.get_transaction_count.side_effect = RuntimeError("rpc down")
    w3.eth.get_transaction_receipt.side_effect = TransactionNotFound("not found")

    pending = list_pending(w3, ACCOUNT)
    assert len(pending) == 1
    assert pending[0].request_id == "r1"


def test_audit_event_enriches_cancel_with_recovery_fields(isolated_files):
    """Fix 4: audit entry for tx cancel records recovery=cancel + old_tx_hash
    so a cancel is distinguishable from a regular 0-value self-send in the log."""
    import json
    from wallet.cli._common import _audit_event
    from wallet.core.policy import Decision
    from wallet.core.tx import PreparedTx

    prepared = PreparedTx(
        tx={"from": ACCOUNT, "to": ACCOUNT, "value": 0, "nonce": 42, "gas": 21000},
        estimated_fee_wei=0,
        description={
            "kind": "tx cancel",
            "is_self_send_for_cancel": True,
            "from": ACCOUNT,
            "to": ACCOUNT,
            "amount_wei": 0,
            "amount_unit": "ETH",
            "amount_decimals": 18,
            "old_tx_hash": "0xdead",
            "cancel_nonce": 42,
        },
    )
    decision = Decision(allowed=True, reason="cancel-allowed", severity="allow")

    _audit_event(
        prepared, SEPOLIA, decision, "agent",
        tx_hash="0xnew", outcome="broadcast", request_id="r1",
    )

    log_lines = (audit.audit_path()).read_text().strip().split("\n")
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["kind"] == "tx_cancel"
    assert entry["recovery"] == "cancel"
    assert entry["old_tx_hash"] == "0xdead"
    assert entry["hash"] == "0xnew"


def test_audit_event_enriches_replace_with_original_kind(isolated_files):
    """Fix 4: a replace entry records original_kind + old_tx_hash so the audit
    log captures what op was being sped up (a fresh send vs aave borrow etc)."""
    import json
    from wallet.cli._common import _audit_event
    from wallet.core.policy import Decision
    from wallet.core.tx import PreparedTx

    prepared = PreparedTx(
        tx={"from": ACCOUNT, "to": OTHER, "value": 100, "nonce": 42, "gas": 21000},
        estimated_fee_wei=0,
        description={
            "kind": "tx replace",
            "is_replacement": True,
            "from": ACCOUNT,
            "to": OTHER,
            "amount_wei": 100,
            "amount_unit": "ETH",
            "amount_decimals": 18,
            "original_tx_hash": "0xorig",
            "original_kind": "native transfer",
            "replace_nonce": 42,
        },
    )
    decision = Decision(allowed=True, reason="ok", severity="allow")

    _audit_event(
        prepared, SEPOLIA, decision, "agent",
        tx_hash="0xrepl", outcome="broadcast", request_id="r1",
    )

    log_lines = (audit.audit_path()).read_text().strip().split("\n")
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["kind"] == "tx_replace"
    assert entry["recovery"] == "replace"
    assert entry["old_tx_hash"] == "0xorig"
    assert entry["original_kind"] == "native transfer"


def test_confirm_and_broadcast_maps_nonce_too_low_to_superseded(isolated_files, monkeypatch):
    """Fix 3: when the original tx mines while a cancel/replace is in flight,
    the RPC returns 'nonce too low'. With `preserve_nonce=True` (only used by
    stuck-tx recovery), this must surface as outcome=superseded, not a raw
    rpc_error — and idempotency + audit must record it cleanly so an agent
    retry sees the same envelope without re-broadcasting."""
    import json
    from wallet.cli import _common
    from wallet.cli._common import confirm_and_broadcast
    from wallet.core.policy import Policy, save_policy
    from wallet.core.tx import PreparedTx
    from wallet.storage.state import AccountEntry, WalletState

    # Policy that allows cancel (the recovery op we'll force into superseded).
    save_policy(Policy(
        max_per_tx={"ETH": "1.0"},
        recipient_allowlist=[ACCOUNT, OTHER],
    ))

    state = WalletState(
        default_chain="sepolia",
        accounts=[AccountEntry(
            name="main", address=ACCOUNT,
            derivation_path="m/44'/60'/0'/0/0", vault_key="k",
            default=True,
        )],
    )

    prepared = PreparedTx(
        tx={
            "from": ACCOUNT, "to": ACCOUNT, "value": 0, "data": "0x",
            "gas": 21000, "nonce": 42, "chainId": 11155111, "type": 2,
            "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**9,
        },
        estimated_fee_wei=21000 * 10**9,
        description={
            "kind": "tx cancel",
            "is_self_send_for_cancel": True,
            "from": ACCOUNT, "to": ACCOUNT,
            "amount_wei": 0, "amount_unit": "ETH", "amount_decimals": 18,
            "cancel_nonce": 42,
            "old_tx_hash": "0xorig",
        },
    )

    # Stub sign + broadcast: broadcast raises "nonce too low" the way an RPC
    # would when the original landed first.
    monkeypatch.setattr(_common, "sign_transaction", lambda *_a, **_kw: b"\x00")
    def _raise_nonce_too_low(*_a, **_kw):
        raise RuntimeError("nonce too low: next nonce 43, tx nonce 42")
    monkeypatch.setattr(_common, "broadcast", _raise_nonce_too_low)

    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    # caller is "agent" so policy/idempotency path is exercised in JSON mode.
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "agent")
    monkeypatch.setenv("WALLET_JSON", "1")

    # Force JSON output mode (set by env var, but emit reads it once)
    from wallet.cli._output import OutputMode
    monkeypatch.setattr(OutputMode, "json", True)

    import typer
    with pytest.raises(typer.Exit) as exc_info:
        confirm_and_broadcast(
            w3, state, SEPOLIA, state.accounts[0], prepared,
            dry_run=False, yes=True,
            request_id="superseded-test",
            preserve_nonce=True,
        )

    # Exit 0 — superseded is a benign race outcome, not a failure.
    assert exc_info.value.exit_code == 0

    # Idempotency entry must record outcome=superseded.
    raw = json.loads((idempotency.store_path()).read_text())
    cached = raw["superseded-test"]
    assert cached["outcome"] == "superseded"
    assert cached["detail"] == "original_landed_first"
    assert cached["tx_hash"] is None

    # Audit entry must say superseded too.
    log_lines = (audit.audit_path()).read_text().strip().split("\n")
    last = json.loads(log_lines[-1])
    assert last["outcome"] == "superseded"
    assert last["kind"] == "tx_cancel"

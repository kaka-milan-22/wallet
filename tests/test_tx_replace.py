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

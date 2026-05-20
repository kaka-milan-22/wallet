"""Tx pipeline: build EIP-1559 fields, simulate, surface revert reasons."""

from unittest.mock import MagicMock

import pytest
from web3 import Web3
from web3.exceptions import ContractLogicError

from wallet.core.config import ChainConfig
from wallet.core.tx import (
    MIN_PRIORITY_GWEI,
    InsufficientFundsError,
    prepare_erc20_approve,
    prepare_native_transfer,
)


SEPOLIA = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="http://invalid",
    explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}",
    native_symbol="ETH",
)

FROM = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
TOKEN = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"


def _w3_mock(*, base_fee_gwei: int = 10, priority_gwei: int = 2, nonce: int = 5):
    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    w3.eth.max_priority_fee = Web3.to_wei(priority_gwei, "gwei")
    w3.eth.get_block.return_value = {"baseFeePerGas": Web3.to_wei(base_fee_gwei, "gwei")}
    w3.eth.get_transaction_count.return_value = nonce
    w3.eth.estimate_gas.return_value = 21000
    w3.eth.call.return_value = b""
    return w3


def test_native_transfer_builds_eip1559():
    w3 = _w3_mock(base_fee_gwei=10, priority_gwei=2, nonce=5)
    pt = prepare_native_transfer(w3, SEPOLIA, FROM, TO, Web3.to_wei(1, "ether"))

    tx = pt.tx
    assert tx["chainId"] == 11155111
    assert tx["type"] == 2
    # nonce is intentionally absent at prepare-time; confirm_and_broadcast
    # refreshes it from "pending" right before signing.
    assert "nonce" not in tx
    assert tx["from"] == FROM
    assert tx["to"] == TO
    assert tx["value"] == Web3.to_wei(1, "ether")
    assert tx["maxPriorityFeePerGas"] == Web3.to_wei(2, "gwei")
    assert tx["maxFeePerGas"] == 2 * Web3.to_wei(10, "gwei") + Web3.to_wei(2, "gwei")
    assert tx["gas"] == 21000
    assert pt.estimated_fee_wei == tx["maxFeePerGas"] * tx["gas"]
    assert pt.description["kind"] == "native transfer"
    assert pt.description["amount_wei"] == Web3.to_wei(1, "ether")


def test_prepared_tx_never_bakes_in_nonce_even_when_builder_fills_it():
    """`Contract.build_transaction(base)` auto-fills nonce from the chain if
    `base` lacks it. We must strip that out so the broadcast path always reads
    a fresh nonce."""
    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    w3.eth.max_priority_fee = Web3.to_wei(2, "gwei")
    w3.eth.get_block.return_value = {"baseFeePerGas": Web3.to_wei(10, "gwei")}
    w3.eth.call.return_value = b""

    fake_contract = MagicMock()
    approve_fn = MagicMock()

    def build_tx(base):
        # Simulate web3.py behavior: builder pre-fills nonce from chain state
        return {**base, "nonce": 42, "to": TOKEN, "data": "0x", "value": 0, "gas": 50000}

    approve_fn.build_transaction.side_effect = build_tx
    fake_contract.functions.approve.return_value = approve_fn
    w3.eth.contract.return_value = fake_contract

    pt = prepare_erc20_approve(w3, SEPOLIA, FROM, TOKEN, TO, 100, "USDC", 6)
    assert "nonce" not in pt.tx, "builder-injected nonce must be stripped at prepare time"


def test_priority_fee_floored_to_one_gwei_when_rpc_returns_zero():
    w3 = _w3_mock(base_fee_gwei=5, priority_gwei=0, nonce=0)
    pt = prepare_native_transfer(w3, SEPOLIA, FROM, TO, 1)
    assert pt.tx["maxPriorityFeePerGas"] == Web3.to_wei(MIN_PRIORITY_GWEI, "gwei")


def test_priority_fee_floored_when_max_priority_fee_unsupported():
    w3 = _w3_mock()
    type(w3.eth).max_priority_fee = property(
        lambda _: (_ for _ in ()).throw(Exception("not supported"))
    )
    pt = prepare_native_transfer(w3, SEPOLIA, FROM, TO, 1)
    assert pt.tx["maxPriorityFeePerGas"] == Web3.to_wei(MIN_PRIORITY_GWEI, "gwei")


def test_simulation_revert_surfaces_clearly():
    w3 = _w3_mock()
    w3.eth.call.side_effect = ContractLogicError("execution reverted: ERC20: insufficient balance")
    with pytest.raises(RuntimeError, match="simulation reverted"):
        prepare_native_transfer(w3, SEPOLIA, FROM, TO, Web3.to_wei(1, "ether"))


def test_finalize_tx_maps_insufficient_funds_to_typed_error():
    """When estimate_gas fails because the sender's balance can't cover
    value + gas, finalize_tx must raise InsufficientFundsError so the CLI
    can emit a clean envelope. Bare RPC tracebacks are non-actionable for
    JSON callers and noisy for humans."""
    w3 = _w3_mock()
    # Simulate geth's exact error message for insufficient funds.
    w3.eth.estimate_gas.side_effect = ValueError({
        "code": -32000,
        "message": "insufficient funds for gas * price + value: "
                   "balance 3310, want 100000000000000000000",
    })

    with pytest.raises(InsufficientFundsError, match="insufficient funds"):
        prepare_native_transfer(w3, SEPOLIA, FROM, TO, Web3.to_wei(100, "ether"))


def test_finalize_tx_does_not_swallow_unrelated_estimate_gas_errors():
    """Only the insufficient-funds class becomes InsufficientFundsError. A
    contract revert during estimate_gas (e.g. require() failure) must keep
    its original type so simulation-revert tests downstream still pass."""
    w3 = _w3_mock()
    w3.eth.estimate_gas.side_effect = ValueError({"code": -32000, "message": "execution reverted: BAD"})

    with pytest.raises(ValueError):
        prepare_native_transfer(w3, SEPOLIA, FROM, TO, Web3.to_wei(1, "ether"))


def test_erc20_approve_builds_with_data_payload():
    w3 = MagicMock(spec=Web3)
    w3.eth = MagicMock()
    w3.eth.max_priority_fee = Web3.to_wei(2, "gwei")
    w3.eth.get_block.return_value = {"baseFeePerGas": Web3.to_wei(10, "gwei")}
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.call.return_value = b""

    fake_contract = MagicMock()
    approve_fn = MagicMock()

    def build_tx(base):
        return {**base, "to": TOKEN, "data": "0x095ea7b3" + "00" * 60, "value": 0, "gas": 50000}

    approve_fn.build_transaction.side_effect = build_tx
    fake_contract.functions.approve.return_value = approve_fn
    w3.eth.contract.return_value = fake_contract

    pt = prepare_erc20_approve(
        w3, SEPOLIA, FROM, TOKEN, TO, 100_000_000, "USDC", 6
    )
    assert pt.tx["to"] == TOKEN
    assert pt.tx["data"].startswith("0x095ea7b3")
    assert pt.tx["value"] == 0
    assert pt.tx["type"] == 2
    assert pt.description["kind"] == "USDC approve"
    assert pt.description["amount_wei"] == 100_000_000

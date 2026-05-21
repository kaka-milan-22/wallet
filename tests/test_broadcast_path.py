"""Broadcast-path disclosure: private_relay vs public_rpc tagging.

Verifies that `confirm_and_broadcast` (a) routes sendRawTransaction through
`web3_broadcast(chain)` rather than the read Web3 instance, and (b) tags the
success envelope with how the broadcast was routed so operators can tell
private-relay submissions (no public mempool exposure, 1-3 block inclusion
delay) from public-rpc submissions at a glance.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from web3 import Web3

from wallet.cli import _common
from wallet.cli._common import _broadcast_path, confirm_and_broadcast
from wallet.core import policy as policy_mod
from wallet.core.config import ChainConfig
from wallet.core.policy import Policy, save_policy
from wallet.core.tx import PreparedTx
from wallet.storage import audit, idempotency
from wallet.storage import state as state_mod
from wallet.storage.state import AccountEntry, WalletState


# --- _broadcast_path helper -------------------------------------------------


def _chain(*, rpc_url, broadcast_rpc_url=None, mev_exposure=False, chain_id=11155111):
    return ChainConfig(
        name="test",
        chain_id=chain_id,
        rpc_url=rpc_url,
        broadcast_rpc_url=broadcast_rpc_url,
        mev_exposure=mev_exposure,
        explorer_api_url="http://invalid",
        explorer_tx_url="http://invalid/{tx}",
        native_symbol="ETH",
    )


def test_broadcast_path_is_private_relay_when_url_set_and_distinct():
    chain = _chain(
        rpc_url="https://eth.llamarpc.com",
        broadcast_rpc_url="https://rpc.flashbots.net",
    )
    assert _broadcast_path(chain) == "private_relay"


def test_broadcast_path_is_public_rpc_when_broadcast_url_unset():
    chain = _chain(rpc_url="https://eth.llamarpc.com", broadcast_rpc_url=None)
    assert _broadcast_path(chain) == "public_rpc"


def test_broadcast_path_is_public_rpc_when_broadcast_equals_read():
    """Split collapsed → effectively public. (Policy gate would already have
    blocked this on mev_exposure chains, but the classifier still reports
    accurately so non-mev_exposure chains can also use the field.)"""
    chain = _chain(
        rpc_url="https://eth.llamarpc.com",
        broadcast_rpc_url="https://eth.llamarpc.com",
    )
    assert _broadcast_path(chain) == "public_rpc"


# --- confirm_and_broadcast: send_raw_transaction routes via web3_broadcast --


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

SEPOLIA_WITH_RELAY = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="http://read.invalid",
    broadcast_rpc_url="http://relay.invalid",
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


def test_confirm_and_broadcast_routes_send_raw_via_web3_broadcast(
    isolated_files, monkeypatch
):
    """Broadcast must use the Web3 instance returned by web3_broadcast(chain),
    not the read w3 passed in. This is the structural read/broadcast split."""
    save_policy(Policy(
        recipient_allowlist=["0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"],
        max_per_tx={"ETH": "1"},
        first_send_warn=False,
    ))
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "tty")

    state = _state_one_account()
    prepared = _self_send_prepared()

    broadcast_w3 = MagicMock(name="broadcast_w3")
    monkeypatch.setattr(_common, "web3_broadcast", lambda chain: broadcast_w3)

    captured: dict = {}

    def fake_broadcast(w3, raw):
        captured["w3"] = w3
        captured["raw"] = raw
        return "0xabc"

    monkeypatch.setattr(_common, "sign_transaction", lambda *a, **kw: b"\x01")
    monkeypatch.setattr(_common, "broadcast", fake_broadcast)

    read_w3 = _stub_w3()
    confirm_and_broadcast(
        read_w3, state, SEPOLIA_WITH_RELAY, state.accounts[0], prepared,
        dry_run=False, yes=True,
        request_id="bp-relay-1",
    )

    # The send_raw_transaction call must go through the broadcast Web3 the
    # factory returned — NOT through the read w3.
    assert captured["w3"] is broadcast_w3
    assert captured["w3"] is not read_w3


def test_confirm_and_broadcast_emits_broadcast_path_field(
    isolated_files, monkeypatch, capsys
):
    """The success envelope must include `broadcast_path` so JSON consumers
    can tell relay submissions apart from public-mempool submissions."""
    save_policy(Policy(
        recipient_allowlist=["0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"],
        max_per_tx={"ETH": "1"},
        first_send_warn=False,
    ))
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "agent")
    monkeypatch.setenv("WALLET_JSON", "1")
    from wallet.cli._output import OutputMode
    monkeypatch.setattr(OutputMode, "json", True)

    state = _state_one_account()
    prepared = _self_send_prepared()

    monkeypatch.setattr(_common, "web3_broadcast", lambda chain: MagicMock())
    monkeypatch.setattr(_common, "sign_transaction", lambda *a, **kw: b"\x01")
    monkeypatch.setattr(_common, "broadcast", lambda *a, **kw: "0xdeadbeef")

    confirm_and_broadcast(
        _stub_w3(), state, SEPOLIA_WITH_RELAY, state.accounts[0], prepared,
        dry_run=False, yes=True,
        request_id="bp-relay-2",
    )

    import json
    out = capsys.readouterr().out.strip().splitlines()[-1]
    envelope = json.loads(out)
    assert envelope["ok"] is True
    assert envelope["data"]["broadcast_path"] == "private_relay"


def test_confirm_and_broadcast_path_is_public_rpc_without_relay(
    isolated_files, monkeypatch, capsys
):
    save_policy(Policy(
        recipient_allowlist=["0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"],
        max_per_tx={"ETH": "1"},
        first_send_warn=False,
    ))
    monkeypatch.setattr("wallet.cli._caller.caller_kind", lambda: "agent")
    monkeypatch.setenv("WALLET_JSON", "1")
    from wallet.cli._output import OutputMode
    monkeypatch.setattr(OutputMode, "json", True)

    state = _state_one_account()
    prepared = _self_send_prepared()

    monkeypatch.setattr(_common, "web3_broadcast", lambda chain: MagicMock())
    monkeypatch.setattr(_common, "sign_transaction", lambda *a, **kw: b"\x01")
    monkeypatch.setattr(_common, "broadcast", lambda *a, **kw: "0xfeedface")

    confirm_and_broadcast(
        _stub_w3(), state, SEPOLIA, state.accounts[0], prepared,
        dry_run=False, yes=True,
        request_id="bp-public-1",
    )

    import json
    out = capsys.readouterr().out.strip().splitlines()[-1]
    envelope = json.loads(out)
    assert envelope["ok"] is True
    assert envelope["data"]["broadcast_path"] == "public_rpc"

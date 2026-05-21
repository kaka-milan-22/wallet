"""make_web3 error wrapping — RPC failures become RpcConnectError, not raw tracebacks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from web3.exceptions import Web3RPCError

from wallet.core.config import ChainConfig
from wallet.core.rpc import RpcConnectError, make_web3, web3_broadcast


CHAIN = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="https://example.invalid/rpc",
    explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}",
    native_symbol="ETH",
)


def _w3_with_chain_id_behavior(*, raise_exc=None, return_id=None):
    """Build a Web3 mock whose `eth.chain_id` either raises or returns a value."""
    w3 = MagicMock()
    if raise_exc is not None:
        type(w3.eth).chain_id = property(lambda self: (_ for _ in ()).throw(raise_exc))
    else:
        type(w3.eth).chain_id = property(lambda self: return_id)
    return w3


def test_make_web3_wraps_http_error_as_rpc_connect_error(monkeypatch):
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = "Unauthorized"
    err = requests.exceptions.HTTPError("401 Unauthorized", response=fake_response)
    w3 = _w3_with_chain_id_behavior(raise_exc=err)

    with patch("wallet.core.rpc.Web3", return_value=w3):
        with pytest.raises(RpcConnectError, match="HTTP 401"):
            make_web3(CHAIN)


def test_make_web3_wraps_connection_error(monkeypatch):
    err = requests.exceptions.ConnectionError("Name resolution failed")
    w3 = _w3_with_chain_id_behavior(raise_exc=err)

    with patch("wallet.core.rpc.Web3", return_value=w3):
        with pytest.raises(RpcConnectError, match="failed to reach RPC"):
            make_web3(CHAIN)


def test_make_web3_wraps_timeout(monkeypatch):
    err = requests.exceptions.Timeout("read timed out")
    w3 = _w3_with_chain_id_behavior(raise_exc=err)

    with patch("wallet.core.rpc.Web3", return_value=w3):
        with pytest.raises(RpcConnectError, match="Timeout"):
            make_web3(CHAIN)


def test_make_web3_wraps_web3_rpc_error(monkeypatch):
    err = Web3RPCError("Method not allowed", rpc_response={"error": {"code": -32601}})
    w3 = _w3_with_chain_id_behavior(raise_exc=err)

    with patch("wallet.core.rpc.Web3", return_value=w3):
        with pytest.raises(RpcConnectError, match="rejected chainId query"):
            make_web3(CHAIN)


def test_make_web3_chain_id_mismatch():
    """RPC reachable but serving the wrong chain → still RpcConnectError."""
    w3 = _w3_with_chain_id_behavior(return_id=1)  # mainnet, but config says sepolia
    with patch("wallet.core.rpc.Web3", return_value=w3):
        with pytest.raises(RpcConnectError, match="chainId mismatch"):
            make_web3(CHAIN)


def test_make_web3_succeeds_on_matching_chain_id():
    w3 = _w3_with_chain_id_behavior(return_id=11155111)
    with patch("wallet.core.rpc.Web3", return_value=w3):
        result = make_web3(CHAIN)
        assert result is w3


# --- url override + validate_chain_id=False ---------------------------------


def test_make_web3_url_override_constructs_provider_with_override_url():
    """`url=` should route HTTPProvider to the override, not chain.rpc_url."""
    captured: dict = {}

    class FakeProvider:
        def __init__(self, url, request_kwargs=None):
            captured["url"] = url
            captured["timeout"] = (request_kwargs or {}).get("timeout")

    w3 = _w3_with_chain_id_behavior(return_id=11155111)
    with patch("wallet.core.rpc.HTTPProvider", FakeProvider), \
         patch("wallet.core.rpc.Web3", return_value=w3):
        make_web3(CHAIN, url="https://override.example/rpc", validate_chain_id=False)

    assert captured["url"] == "https://override.example/rpc"
    assert captured["timeout"] == 20


def test_make_web3_validate_chain_id_false_skips_chain_id_call():
    """With validate_chain_id=False, eth.chain_id must never be touched —
    private relays don't answer this method."""
    w3 = MagicMock()
    chain_id_accesses = []
    type(w3.eth).chain_id = property(
        lambda self: chain_id_accesses.append(1) or (_ for _ in ()).throw(
            RuntimeError("eth.chain_id should not be called when validate_chain_id=False")
        )
    )
    with patch("wallet.core.rpc.Web3", return_value=w3):
        result = make_web3(CHAIN, validate_chain_id=False)
    assert result is w3
    assert chain_id_accesses == []


# --- web3_broadcast factory --------------------------------------------------


def test_web3_broadcast_uses_broadcast_rpc_url_when_set():
    chain = ChainConfig(
        name="ethereum",
        chain_id=1,
        rpc_url="https://eth.llamarpc.com",
        broadcast_rpc_url="https://rpc.flashbots.net",
        explorer_api_url="https://api.etherscan.io/v2/api",
        explorer_tx_url="https://etherscan.io/tx/{tx}",
        native_symbol="ETH",
    )
    captured: dict = {}

    class FakeProvider:
        def __init__(self, url, request_kwargs=None):
            captured["url"] = url

    w3 = MagicMock()
    type(w3.eth).chain_id = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("must not call chain_id"))
    )
    with patch("wallet.core.rpc.HTTPProvider", FakeProvider), \
         patch("wallet.core.rpc.Web3", return_value=w3):
        web3_broadcast(chain)
    assert captured["url"] == "https://rpc.flashbots.net"


def test_web3_broadcast_falls_back_to_rpc_url_when_broadcast_unset():
    chain = ChainConfig(
        name="sepolia",
        chain_id=11155111,
        rpc_url="https://ethereum-sepolia.publicnode.com",
        broadcast_rpc_url=None,
        mev_exposure=False,
        explorer_api_url="https://api.etherscan.io/v2/api",
        explorer_tx_url="https://sepolia.etherscan.io/tx/{tx}",
        native_symbol="ETH",
    )
    captured: dict = {}

    class FakeProvider:
        def __init__(self, url, request_kwargs=None):
            captured["url"] = url

    w3 = MagicMock()
    type(w3.eth).chain_id = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("must not call chain_id"))
    )
    with patch("wallet.core.rpc.HTTPProvider", FakeProvider), \
         patch("wallet.core.rpc.Web3", return_value=w3):
        web3_broadcast(chain)
    assert captured["url"] == "https://ethereum-sepolia.publicnode.com"


def test_web3_broadcast_never_probes_chain_id():
    """Private relays only expose sendRawTransaction. web3_broadcast must
    skip chain_id validation regardless of whether broadcast_rpc_url is set."""
    chain = ChainConfig(
        name="ethereum",
        chain_id=1,
        rpc_url="https://eth.llamarpc.com",
        broadcast_rpc_url="https://rpc.flashbots.net",
        explorer_api_url="https://api.etherscan.io/v2/api",
        explorer_tx_url="https://etherscan.io/tx/{tx}",
        native_symbol="ETH",
    )
    w3 = MagicMock()
    type(w3.eth).chain_id = property(
        lambda self: (_ for _ in ()).throw(
            RuntimeError("web3_broadcast must not query eth_chainId")
        )
    )
    with patch("wallet.core.rpc.Web3", return_value=w3):
        result = web3_broadcast(chain)
    assert result is w3

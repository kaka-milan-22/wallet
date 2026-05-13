"""make_web3 error wrapping — RPC failures become RpcConnectError, not raw tracebacks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from web3.exceptions import Web3RPCError

from wallet.core.config import ChainConfig
from wallet.core.rpc import RpcConnectError, make_web3


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

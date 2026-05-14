"""Tier 1.3 — fetch_token_info caches by (chain_id, address) so portfolio /
swap / resolve_token paths don't hit the RPC for the same ERC-20 metadata
multiple times within a session."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wallet.core import tokens
from wallet.core.tokens import TokenInfo, clear_token_info_cache, fetch_token_info


ADDR = "0x" + "ab" * 20


@pytest.fixture(autouse=True)
def _clear():
    clear_token_info_cache()
    yield
    clear_token_info_cache()


def _w3(chain_id: int = 11155111, symbol: str = "USDC", decimals: int = 6) -> MagicMock:
    w3 = MagicMock()
    w3.eth.chain_id = chain_id

    fake_contract = MagicMock()
    fake_contract.functions.symbol.return_value.call.return_value = symbol
    fake_contract.functions.decimals.return_value.call.return_value = decimals
    w3.eth.contract.return_value = fake_contract

    # Mirror Web3.to_checksum_address; we patch the *module* attr below.
    return w3


def test_first_call_fetches_from_chain(monkeypatch):
    w3 = _w3()
    info = fetch_token_info(w3, ADDR)
    assert isinstance(info, TokenInfo)
    assert info.symbol == "USDC"
    assert info.decimals == 6
    assert w3.eth.contract.call_count == 1


def test_repeated_call_returns_from_cache(monkeypatch):
    w3 = _w3()
    fetch_token_info(w3, ADDR)
    fetch_token_info(w3, ADDR)
    fetch_token_info(w3, ADDR)
    # Only one chain round-trip total
    assert w3.eth.contract.call_count == 1


def test_different_chain_ids_do_not_collide(monkeypatch):
    a = _w3(chain_id=1, symbol="USDC-mainnet", decimals=6)
    b = _w3(chain_id=11155111, symbol="USDC-sepolia", decimals=6)

    info_a = fetch_token_info(a, ADDR)
    info_b = fetch_token_info(b, ADDR)
    assert info_a.symbol == "USDC-mainnet"
    assert info_b.symbol == "USDC-sepolia"

    # Both fetched once each, neither cross-poisoned
    fetch_token_info(a, ADDR)
    fetch_token_info(b, ADDR)
    assert a.eth.contract.call_count == 1
    assert b.eth.contract.call_count == 1


def test_mocked_w3_without_chain_id_does_not_cache(monkeypatch):
    """If chain_id is unobtainable (offline mock / disconnected), we should
    still return correct data but not poison the cache for real sessions."""
    w3 = _w3()
    w3.eth.chain_id = property(
        lambda _: (_ for _ in ()).throw(Exception("no chain id"))
    )

    # Will fall through to chain_id=0 sentinel and skip cache.
    info1 = fetch_token_info(w3, ADDR)
    info2 = fetch_token_info(w3, ADDR)
    assert info1.symbol == info2.symbol
    # No cache → both calls go to chain
    assert w3.eth.contract.call_count == 2

"""Portfolio command — token gather + concurrent balance fetch."""

from __future__ import annotations

from unittest.mock import MagicMock


from wallet.cli.portfolio import _Token, _fetch_balances_for, _gather_tokens
from wallet.core.config import ChainConfig
from wallet.storage.state import TokenEntry, WalletState


def _chain_with_builtins(builtins: dict[str, str]) -> ChainConfig:
    return ChainConfig(
        name="sepolia",
        chain_id=11155111,
        rpc_url="http://invalid",
        explorer_api_url="http://invalid",
        explorer_tx_url="http://invalid/{tx}",
        native_symbol="ETH",
        builtin_tokens=builtins,
    )


def test_gather_tokens_puts_native_first(monkeypatch):
    chain = _chain_with_builtins({"USDC": "0x" + "11" * 20})
    state = WalletState()

    # fetch_token_info is called for builtins — mock it
    from wallet.core import tokens
    from wallet.core.tokens import TokenInfo
    monkeypatch.setattr(
        tokens, "fetch_token_info",
        lambda w3, addr: TokenInfo(symbol="USDC", address=addr, decimals=6),
    )
    # The portfolio module imports `fetch_token_info` directly, so patch there too
    from wallet.cli import portfolio as portfolio_mod
    monkeypatch.setattr(
        portfolio_mod, "fetch_token_info",
        lambda w3, addr: TokenInfo(symbol="USDC", address=addr, decimals=6),
    )

    result = _gather_tokens(w3=MagicMock(), cfg=chain, state=state)
    assert result[0].source == "native"
    assert result[0].symbol == "ETH"
    assert result[1].source == "builtin"
    assert result[1].symbol == "USDC"


def test_gather_tokens_includes_user_tokens(monkeypatch):
    chain = _chain_with_builtins({})
    state = WalletState(tokens=[
        TokenEntry(symbol="LINK", address="0x" + "22" * 20, decimals=18, chain="sepolia"),
    ])

    from wallet.cli import portfolio as portfolio_mod
    monkeypatch.setattr(portfolio_mod, "fetch_token_info",
                        lambda w3, addr: (_ for _ in ()).throw(AssertionError("should not be called for no builtins")))

    result = _gather_tokens(w3=MagicMock(), cfg=chain, state=state)
    assert [(t.symbol, t.source) for t in result] == [("ETH", "native"), ("LINK", "user")]


def test_gather_tokens_skips_user_tokens_from_other_chains(monkeypatch):
    chain = _chain_with_builtins({})
    state = WalletState(tokens=[
        TokenEntry(symbol="LINK", address="0x" + "22" * 20, decimals=18, chain="mainnet"),
    ])

    from wallet.cli import portfolio as portfolio_mod
    monkeypatch.setattr(portfolio_mod, "fetch_token_info", lambda w3, addr: None)

    result = _gather_tokens(w3=MagicMock(), cfg=chain, state=state)
    # Only native; the LINK on "mainnet" is filtered out for sepolia config
    assert [t.symbol for t in result] == ["ETH"]


def test_fetch_balances_collects_native_and_erc20(monkeypatch):
    """Verify native goes through eth.get_balance and tokens go through balance_of."""
    addr = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

    w3 = MagicMock()
    w3.eth.get_balance.return_value = 5 * 10**18  # 5 ETH

    from wallet.cli import portfolio as portfolio_mod
    monkeypatch.setattr(
        portfolio_mod, "balance_of",
        lambda w3_, token_addr, owner: 100 * 10**6 if token_addr.endswith("11" * 20) else 0,
    )

    tokens = [
        _Token(symbol="ETH", address="", decimals=18, source="native"),
        _Token(symbol="USDC", address="0x" + "11" * 20, decimals=6, source="builtin"),
        _Token(symbol="WETH", address="0x" + "22" * 20, decimals=18, source="builtin"),
    ]

    rows = _fetch_balances_for(w3, addr, tokens)

    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["ETH"]["amount_wei"] == str(5 * 10**18)
    assert by_sym["ETH"]["amount"] == "5"
    assert by_sym["USDC"]["amount"] == "100"
    assert by_sym["WETH"]["amount"] == "0"
    # Ordering preserved
    assert [r["symbol"] for r in rows] == ["ETH", "USDC", "WETH"]

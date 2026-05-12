"""Portfolio view: native + all configured tokens for one or many accounts.

Each balance is fetched concurrently via a thread pool so a 10-token portfolio
takes ~one RPC RTT total instead of N×RTT.

Tokens queried:
  - chain.native (e.g. ETH) — `w3.eth.get_balance`
  - chain.builtin_tokens (USDC, WETH on Sepolia) — ERC-20 `balanceOf`
  - state.tokens that match the active chain — ERC-20 `balanceOf`

Zero-balance entries are included so the table doesn't change shape across
calls — agents can pivot the JSON without worrying about missing fields.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import typer
from rich.table import Table
from web3 import Web3

from wallet.cli._output import emit, emit_error, info, stdout_console
from wallet.core.config import get_chain
from wallet.core.rpc import format_units, make_web3
from wallet.core.tokens import balance_of, fetch_token_info
from wallet.storage.state import load_state


@dataclass(frozen=True)
class _Token:
    symbol: str
    address: str
    decimals: int
    source: str  # "native" / "builtin" / "user"


def _gather_tokens(w3, cfg, state) -> list[_Token]:
    """Build the unified token list for the active chain.

    Native goes first; then builtins (USDC / WETH); then user-registered
    tokens. Builtin decimals are fetched on-chain because `builtin_tokens`
    is just a {symbol: address} map; user tokens already cache decimals.
    """
    tokens: list[_Token] = []

    # Native
    tokens.append(_Token(symbol=cfg.native_symbol, address="", decimals=18, source="native"))

    # Builtins — fetch decimals via on-chain `decimals()` call
    for sym, addr in cfg.builtin_tokens.items():
        info_ = fetch_token_info(w3, addr)
        tokens.append(_Token(
            symbol=sym, address=info_.address, decimals=info_.decimals, source="builtin",
        ))

    # User-registered (cached decimals)
    seen = {t.address.lower() for t in tokens if t.address}
    for t in state.tokens:
        if t.chain != cfg.name:
            continue
        if t.address.lower() in seen:
            continue  # user re-added a builtin; show the builtin entry only
        tokens.append(_Token(
            symbol=t.symbol, address=t.address, decimals=t.decimals, source="user",
        ))

    return tokens


def _fetch_balances_for(w3, addr_checksum: str, tokens: list[_Token]) -> list[dict]:
    """Concurrently fetch every token balance for one account address.

    Returns a list of dicts (one per token) in the same order as `tokens`.
    """
    results: list[dict] = [None] * len(tokens)  # type: ignore[list-item]

    def fetch(i: int, tok: _Token):
        if tok.source == "native":
            amt = int(w3.eth.get_balance(addr_checksum))
        else:
            amt = int(balance_of(w3, tok.address, addr_checksum))
        return i, amt

    with ThreadPoolExecutor(max_workers=min(10, max(1, len(tokens)))) as pool:
        for i, amt in pool.map(lambda x: fetch(*x), enumerate(tokens)):
            tok = tokens[i]
            results[i] = {
                "symbol": tok.symbol,
                "address": tok.address or None,
                "decimals": tok.decimals,
                "source": tok.source,
                "amount_wei": str(amt),
                "amount": format_units(amt, tok.decimals),
            }

    return results


def portfolio(
    account: str | None = typer.Option(None, "--account", "-a", help="Account name (default: current default)"),
    all_accounts: bool = typer.Option(False, "--all", help="Show every registered account + watched address"),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    """Show native + all known tokens balances for one or many accounts."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)

    # Resolve which accounts to query
    if all_accounts:
        targets: list[tuple[str, str]] = [(a.name, a.address) for a in state.accounts]
        targets += [(w.label or w.address[:10], w.address) for w in state.watch]
    elif account:
        a = state.find_account(account)
        if a:
            targets = [(a.name, a.address)]
        else:
            for w in state.watch:
                if w.label == account or w.address.lower() == account.lower():
                    targets = [(w.label or w.address, w.address)]
                    break
            else:
                emit_error("validation_error", command="portfolio", chain=cfg.name,
                           reason=f"no account or watched address: {account}")
                raise typer.Exit(code=2)
    else:
        a = state.get_default_account()
        if not a:
            emit_error("validation_error", command="portfolio", chain=cfg.name,
                       reason="no accounts registered — run `wallet account create <name>`")
            raise typer.Exit(code=2)
        targets = [(a.name, a.address)]

    if not targets:
        emit_error("validation_error", command="portfolio", chain=cfg.name,
                   reason="no accounts to query")
        raise typer.Exit(code=2)

    # Gather the token list once (decimals etc.)
    tokens = _gather_tokens(w3, cfg, state)

    accounts_data: list[dict] = []
    for label, addr in targets:
        addr_cs = Web3.to_checksum_address(addr)
        balances = _fetch_balances_for(w3, addr_cs, tokens)
        accounts_data.append({"label": label, "address": addr_cs, "balances": balances})

    data = {
        "ok": True,
        "command": "portfolio",
        "chain": cfg.name,
        "data": {
            "tokens_tracked": len(tokens),
            "accounts": accounts_data,
        },
    }

    def render(d):
        accounts = d["data"]["accounts"]
        if not accounts:
            info("[dim]no accounts to show[/dim]")
            return
        for acct in accounts:
            table = Table(
                title=f"[bold]{acct['label']}[/bold] [dim]({acct['address']})[/dim] on [cyan]{d['chain']}[/cyan]",
                show_header=True, header_style="bold",
            )
            table.add_column("symbol")
            table.add_column("amount", justify="right")
            table.add_column("decimals", justify="right", style="dim")
            table.add_column("source", style="dim")
            table.add_column("address", style="dim")
            for b in acct["balances"]:
                amount_style = "" if int(b["amount_wei"]) > 0 else "dim"
                table.add_row(
                    f"[{amount_style}]{b['symbol']}[/{amount_style}]" if amount_style else b["symbol"],
                    f"[{amount_style}]{b['amount']}[/{amount_style}]" if amount_style else b["amount"],
                    str(b["decimals"]),
                    b["source"],
                    b["address"] or "—",
                )
            stdout_console().print(table)
            if len(accounts) > 1:
                stdout_console().print()  # blank line between accounts

    emit(data, render)

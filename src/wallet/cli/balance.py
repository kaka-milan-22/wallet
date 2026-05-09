from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from wallet.core.config import get_chain
from wallet.core.rpc import format_units, make_web3
from wallet.core.tokens import balance_of, resolve_token
from wallet.storage.state import load_state

console = Console()


def _resolve_targets(state, account: str | None, all_watched: bool) -> list[tuple[str, str]]:
    """Return list of (label, address) to query."""
    if all_watched:
        rows: list[tuple[str, str]] = [(a.name, a.address) for a in state.accounts]
        rows += [(w.label or w.address[:10], w.address) for w in state.watch]
        return rows

    if account:
        a = state.find_account(account)
        if a:
            return [(a.name, a.address)]
        # treat as watch label or address
        for w in state.watch:
            if w.label == account or w.address.lower() == account.lower():
                return [(w.label or w.address, w.address)]
        raise typer.BadParameter(f"no account or watched address: {account}")

    a = state.get_default_account()
    if not a:
        raise typer.BadParameter(
            "no accounts registered — run `wallet account create <name>`"
        )
    return [(a.name, a.address)]


def balance(
    account: str | None = typer.Option(None, "--account", "-a", help="Account name (default: current default)"),
    token: str | None = typer.Option(None, "--token", "-t", help="Token symbol or 0x address (default: native)"),
    all_watched: bool = typer.Option(False, "--all", help="Show every account + watched address"),
    chain: str = typer.Option(None, "--chain", help="Chain name (default: state.default_chain)"),
) -> None:
    """Show native or ERC-20 balance for one or more addresses."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)

    targets = _resolve_targets(state, account, all_watched)

    if token:
        info = resolve_token(w3, cfg, state, token)
        unit = info.symbol
        decimals = info.decimals
        fetcher = lambda addr: balance_of(w3, info.address, addr)
        header_extra = f" [dim]({info.address})[/dim]"
    else:
        unit = cfg.native_symbol
        decimals = 18
        fetcher = lambda addr: w3.eth.get_balance(w3.to_checksum_address(addr))
        header_extra = ""

    table = Table(
        title=f"{unit}{header_extra} on [cyan]{cfg.name}[/cyan]",
        show_header=True,
        header_style="bold",
    )
    table.add_column("label")
    table.add_column("address", style="dim")
    table.add_column(f"balance ({unit})", justify="right")

    for label, addr in targets:
        amt = fetcher(addr)
        table.add_row(label, addr, format_units(amt, decimals))

    console.print(table)

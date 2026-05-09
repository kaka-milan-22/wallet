from __future__ import annotations

from datetime import datetime, timezone

import typer
from rich.console import Console
from rich.table import Table
from web3 import Web3

from wallet.core.config import get_chain
from wallet.core.rpc import format_units
from wallet.services.explorer import EtherscanError, list_native_txs, list_token_txs
from wallet.storage.state import load_state

console = Console()


def _resolve_target(state, account: str | None, address: str | None) -> tuple[str, str]:
    if address:
        return Web3.to_checksum_address(address), address[:10]
    if account:
        a = state.find_account(account)
        if a:
            return a.address, a.name
        for w in state.watch:
            if w.label == account:
                return Web3.to_checksum_address(w.address), w.label or account
        raise typer.BadParameter(f"unknown account or watch label: {account}")
    a = state.get_default_account()
    if not a:
        raise typer.BadParameter("no default account; pass --account or --address")
    return a.address, a.name


def _direction(target: str, frm: str, to: str) -> str:
    t = target.lower()
    if frm.lower() == t:
        return "[red]OUT[/red]"
    if (to or "").lower() == t:
        return "[green]IN [/green]"
    return "    "


def _ts(s: str) -> str:
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return s


def history(
    account: str | None = typer.Option(None, "--account", "-a"),
    address: str | None = typer.Option(None, "--address"),
    limit: int = typer.Option(20, "--limit", "-n"),
    tokens: bool = typer.Option(False, "--tokens", help="Show ERC-20 transfers instead of native txs"),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    """Show recent transactions via Etherscan v2 API."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    target_addr, target_label = _resolve_target(state, account, address)

    try:
        txs = (
            list_token_txs(cfg, target_addr, limit=limit)
            if tokens
            else list_native_txs(cfg, target_addr, limit=limit)
        )
    except EtherscanError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)

    if not txs:
        console.print(f"[dim]no transactions for {target_label} ({target_addr})[/dim]")
        return

    table = Table(
        title=f"{'token transfers' if tokens else 'transactions'} for "
              f"[bold]{target_label}[/bold] ({target_addr}) on [cyan]{cfg.name}[/cyan]",
        show_header=True,
        header_style="bold",
    )
    table.add_column("when", style="dim")
    table.add_column("dir")
    table.add_column("counterparty", style="dim")
    table.add_column("amount", justify="right")
    table.add_column("status")
    table.add_column("hash", style="dim")

    for t in txs:
        frm = t.get("from", "")
        to = t.get("to", "")
        counter = to if frm.lower() == target_addr.lower() else frm

        if tokens:
            decimals = int(t.get("tokenDecimal", "18"))
            symbol = t.get("tokenSymbol", "?")
            amount = format_units(int(t.get("value", "0")), decimals)
            amount_str = f"{amount} {symbol}"
            status = ""  # tokentx endpoint doesn't return per-tx error; presence implies success
        else:
            amount = format_units(int(t.get("value", "0")), 18)
            amount_str = f"{amount} {cfg.native_symbol}"
            ok = t.get("txreceipt_status", "1") == "1" and t.get("isError", "0") == "0"
            status = "[green]ok[/green]" if ok else "[red]revert[/red]"

        table.add_row(
            _ts(t.get("timeStamp", "")),
            _direction(target_addr, frm, to),
            f"{counter[:10]}…{counter[-6:]}" if counter else "-",
            amount_str,
            status,
            t.get("hash", "")[:12] + "…",
        )

    console.print(table)

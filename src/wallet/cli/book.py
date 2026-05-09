from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table
from web3 import Web3

from wallet.storage.state import load_state, save_state

app = typer.Typer(no_args_is_help=True, help="Address book — alias → address")
console = Console()


@app.command("add")
def add(
    alias: str = typer.Argument(..., help="Short name to remember (use as @alias)"),
    address: str = typer.Argument(..., help="0x address"),
) -> None:
    """Add or overwrite an alias → address mapping."""
    if not address.startswith("0x") or len(address) != 42:
        raise typer.BadParameter("address must be a 0x-prefixed 40-hex-digit string")
    addr = Web3.to_checksum_address(address)
    state = load_state()
    existed = alias in state.book
    state.book[alias] = addr
    save_state(state)
    console.print(
        f"[{'yellow' if existed else 'green'}]"
        f"{'updated' if existed else 'added'}[/] @{alias} → {addr}"
    )


@app.command("list")
def list_() -> None:
    state = load_state()
    if not state.book:
        console.print("[dim]address book is empty[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("alias")
    table.add_column("address")
    for alias, addr in sorted(state.book.items()):
        table.add_row(f"@{alias}", addr)
    console.print(table)


@app.command("remove")
def remove(alias: str = typer.Argument(...)) -> None:
    state = load_state()
    if alias not in state.book:
        console.print(f"[red]no alias: @{alias}[/red]")
        raise typer.Exit(code=1)
    del state.book[alias]
    save_state(state)
    console.print(f"removed @{alias}")

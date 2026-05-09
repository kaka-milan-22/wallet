from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table
from web3 import Web3

from wallet.storage.state import WatchEntry, load_state, save_state

app = typer.Typer(no_args_is_help=True, help="Watch-only addresses (no private keys held)")
console = Console()


@app.command("add")
def add(
    address: str = typer.Argument(..., help="0x address to watch"),
    label: str | None = typer.Option(None, "--label", "-l"),
) -> None:
    if not address.startswith("0x") or len(address) != 42:
        raise typer.BadParameter("address must be a 0x-prefixed 40-hex-digit string")
    addr = Web3.to_checksum_address(address)
    state = load_state()
    if any(w.address.lower() == addr.lower() for w in state.watch):
        console.print(f"[yellow]already watching {addr}[/yellow]")
        return
    state.watch.append(WatchEntry(address=addr, label=label))
    save_state(state)
    console.print(f"[green]watching[/green] {addr}{f' [{label}]' if label else ''}")


@app.command("list")
def list_() -> None:
    state = load_state()
    if not state.watch:
        console.print("[dim]no watched addresses[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("label")
    table.add_column("address")
    for w in state.watch:
        table.add_row(w.label or "", w.address)
    console.print(table)


@app.command("remove")
def remove(target: str = typer.Argument(..., help="Address or label")) -> None:
    state = load_state()
    before = len(state.watch)
    state.watch = [
        w for w in state.watch
        if w.address.lower() != target.lower() and w.label != target
    ]
    if len(state.watch) == before:
        console.print(f"[red]no watch entry matching: {target}[/red]")
        raise typer.Exit(code=1)
    save_state(state)
    console.print(f"removed {target}")

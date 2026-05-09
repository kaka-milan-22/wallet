from __future__ import annotations

import typer
from rich.table import Table
from web3 import Web3

from wallet.cli._output import emit, emit_error, info, stdout_console
from wallet.storage.state import load_state, save_state

app = typer.Typer(no_args_is_help=True, help="Address book — alias → address")


def _validate_addr(address: str) -> str:
    if not address.startswith("0x") or len(address) != 42:
        raise ValueError("address must be a 0x-prefixed 40-hex-digit string")
    return Web3.to_checksum_address(address)


@app.command("add")
def add(
    alias: str = typer.Argument(..., help="Short name to remember (use as @alias)"),
    address: str = typer.Argument(..., help="0x address"),
) -> None:
    """Add or overwrite an alias → address mapping."""
    try:
        addr = _validate_addr(address)
    except ValueError as e:
        emit_error("validation_error", command="book.add", reason=str(e))
        raise typer.Exit(code=2)

    state = load_state()
    existed = alias in state.book
    state.book[alias] = addr
    save_state(state)

    data = {
        "ok": True,
        "command": "book.add",
        "data": {"alias": alias, "address": addr, "updated": existed},
    }
    emit(data, lambda d: stdout_console().print(
        f"[{'yellow' if d['data']['updated'] else 'green'}]"
        f"{'updated' if d['data']['updated'] else 'added'}[/] @{d['data']['alias']} → {d['data']['address']}"
    ))


@app.command("list")
def list_() -> None:
    """List all aliases."""
    state = load_state()
    entries = [{"alias": a, "address": addr} for a, addr in sorted(state.book.items())]
    data = {"ok": True, "command": "book.list", "data": {"entries": entries}}

    def render(d):
        if not d["data"]["entries"]:
            info("[dim]address book is empty[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("alias")
        table.add_column("address")
        for e in d["data"]["entries"]:
            table.add_row(f"@{e['alias']}", e["address"])
        stdout_console().print(table)

    emit(data, render)


@app.command("remove")
def remove(alias: str = typer.Argument(...)) -> None:
    """Remove an alias."""
    state = load_state()
    if alias not in state.book:
        emit_error("not_found", command="book.remove", reason=f"no alias: @{alias}")
        raise typer.Exit(code=1)
    del state.book[alias]
    save_state(state)
    emit(
        {"ok": True, "command": "book.remove", "data": {"alias": alias}},
        lambda d: stdout_console().print(f"removed @{d['data']['alias']}"),
    )

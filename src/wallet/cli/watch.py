from __future__ import annotations

import typer
from rich.table import Table
from web3 import Web3

from wallet.cli._output import emit, emit_error, info, stdout_console
from wallet.storage.state import WatchEntry, load_state, save_state

app = typer.Typer(no_args_is_help=True, help="Watch-only addresses (no private keys held)")


@app.command("add")
def add(
    address: str = typer.Argument(..., help="0x address to watch"),
    label: str | None = typer.Option(None, "--label", "-l"),
) -> None:
    if not address.startswith("0x") or len(address) != 42:
        emit_error("validation_error", command="watch.add",
                   reason="address must be a 0x-prefixed 40-hex-digit string")
        raise typer.Exit(code=2)
    addr = Web3.to_checksum_address(address)

    state = load_state()
    if any(w.address.lower() == addr.lower() for w in state.watch):
        emit(
            {"ok": True, "command": "watch.add",
             "data": {"address": addr, "label": label, "duplicate": True}},
            lambda d: info(f"[yellow]already watching {d['data']['address']}[/yellow]"),
        )
        return
    state.watch.append(WatchEntry(address=addr, label=label))
    save_state(state)

    def _render_added(d):
        x = d["data"]
        label_part = f" [{x['label']}]" if x["label"] else ""
        stdout_console().print(f"[green]watching[/green] {x['address']}{label_part}")

    emit(
        {"ok": True, "command": "watch.add",
         "data": {"address": addr, "label": label, "duplicate": False}},
        _render_added,
    )


@app.command("list")
def list_() -> None:
    state = load_state()
    entries = [{"label": w.label, "address": w.address} for w in state.watch]
    data = {"ok": True, "command": "watch.list", "data": {"entries": entries}}

    def render(d):
        if not d["data"]["entries"]:
            info("[dim]no watched addresses[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("label")
        table.add_column("address")
        for e in d["data"]["entries"]:
            table.add_row(e["label"] or "", e["address"])
        stdout_console().print(table)

    emit(data, render)


@app.command("remove")
def remove(target: str = typer.Argument(..., help="Address or label")) -> None:
    state = load_state()
    before = len(state.watch)
    state.watch = [
        w for w in state.watch
        if w.address.lower() != target.lower() and w.label != target
    ]
    if len(state.watch) == before:
        emit_error("not_found", command="watch.remove", reason=f"no watch entry matching: {target}")
        raise typer.Exit(code=1)
    save_state(state)
    emit(
        {"ok": True, "command": "watch.remove", "data": {"removed": target}},
        lambda d: stdout_console().print(f"removed {d['data']['removed']}"),
    )

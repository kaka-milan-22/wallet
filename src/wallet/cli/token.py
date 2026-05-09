from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from wallet.core.config import get_chain
from wallet.core.rpc import make_web3
from wallet.core.tokens import fetch_token_info
from wallet.storage.state import TokenEntry, load_state, save_state

app = typer.Typer(no_args_is_help=True, help="Track ERC-20 tokens by symbol")
console = Console()


@app.command("add")
def add(
    address: str = typer.Argument(..., help="ERC-20 contract address"),
    symbol: str | None = typer.Option(None, "--symbol", "-s", help="Override on-chain symbol"),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    """Register a token. Decimals and symbol are read from the contract."""
    if not address.startswith("0x") or len(address) != 42:
        raise typer.BadParameter("address must be 0x-prefixed 40-hex-digit string")
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)
    info = fetch_token_info(w3, address)
    sym = (symbol or info.symbol).upper()

    state.tokens = [t for t in state.tokens if not (t.symbol.upper() == sym and t.chain == cfg.name)]
    state.tokens.append(
        TokenEntry(symbol=sym, address=info.address, decimals=info.decimals, chain=cfg.name)
    )
    save_state(state)
    console.print(f"[green]added[/green] {sym} ({info.address}, {info.decimals} decimals) on {cfg.name}")


@app.command("list")
def list_(chain: str | None = typer.Option(None, "--chain")) -> None:
    state = load_state()
    cfg_name = chain or state.default_chain
    cfg = get_chain(cfg_name)
    rows = []
    for sym, addr in cfg.builtin_tokens.items():
        rows.append((sym, addr, "—", "builtin"))
    for t in state.tokens:
        if t.chain == cfg_name:
            rows.append((t.symbol, t.address, str(t.decimals), "user"))

    if not rows:
        console.print(f"[dim]no tokens on {cfg_name}[/dim]")
        return

    table = Table(title=f"tokens on [cyan]{cfg_name}[/cyan]", show_header=True, header_style="bold")
    table.add_column("symbol")
    table.add_column("address", style="dim")
    table.add_column("decimals", justify="right")
    table.add_column("source")
    for sym, addr, dec, src in rows:
        table.add_row(sym, addr, dec, src)
    console.print(table)


@app.command("remove")
def remove(
    symbol: str = typer.Argument(...),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    state = load_state()
    cfg_name = chain or state.default_chain
    sym = symbol.upper()
    before = len(state.tokens)
    state.tokens = [t for t in state.tokens if not (t.symbol.upper() == sym and t.chain == cfg_name)]
    if len(state.tokens) == before:
        console.print(f"[red]no user token {sym} on {cfg_name}[/red]")
        raise typer.Exit(code=1)
    save_state(state)
    console.print(f"removed {sym} on {cfg_name}")

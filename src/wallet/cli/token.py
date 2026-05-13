from __future__ import annotations

import typer
from rich.table import Table

from wallet.cli._output import emit, emit_error, info, stdout_console
from wallet.core.config import get_chain
from wallet.cli._common import make_web3_or_exit
from wallet.core.tokens import fetch_token_info
from wallet.storage.state import TokenEntry, load_state, save_state

app = typer.Typer(no_args_is_help=True, help="Track ERC-20 tokens by symbol")


@app.command("add")
def add(
    address: str = typer.Argument(..., help="ERC-20 contract address"),
    symbol: str | None = typer.Option(None, "--symbol", "-s", help="Override on-chain symbol"),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    """Register a token. Decimals and symbol are read from the contract."""
    if not address.startswith("0x") or len(address) != 42:
        emit_error("validation_error", command="token.add",
                   reason="address must be 0x-prefixed 40-hex-digit string")
        raise typer.Exit(code=2)

    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="token.add")
    try:
        info_ = fetch_token_info(w3, address)
    except Exception as e:
        emit_error("rpc_error", command="token.add", chain=cfg.name,
                   reason=f"{type(e).__name__}: {e}")
        raise typer.Exit(code=1)

    sym = (symbol or info_.symbol).upper()
    state.tokens = [t for t in state.tokens if not (t.symbol.upper() == sym and t.chain == cfg.name)]
    state.tokens.append(
        TokenEntry(symbol=sym, address=info_.address, decimals=info_.decimals, chain=cfg.name)
    )
    save_state(state)

    data = {
        "ok": True, "command": "token.add", "chain": cfg.name,
        "data": {"symbol": sym, "address": info_.address, "decimals": info_.decimals},
    }
    emit(data, lambda d: stdout_console().print(
        f"[green]added[/green] {d['data']['symbol']} ({d['data']['address']}, "
        f"{d['data']['decimals']} decimals) on {d['chain']}"
    ))


@app.command("list")
def list_(chain: str | None = typer.Option(None, "--chain")) -> None:
    state = load_state()
    cfg_name = chain or state.default_chain
    cfg = get_chain(cfg_name)
    builtins = [
        {"symbol": s, "address": a, "decimals": None, "source": "builtin"}
        for s, a in cfg.builtin_tokens.items()
    ]
    user = [
        {"symbol": t.symbol, "address": t.address, "decimals": t.decimals, "source": "user"}
        for t in state.tokens if t.chain == cfg_name
    ]
    tokens = builtins + user
    data = {"ok": True, "command": "token.list", "chain": cfg_name, "data": {"tokens": tokens}}

    def render(d):
        if not d["data"]["tokens"]:
            info(f"[dim]no tokens on {d['chain']}[/dim]")
            return
        table = Table(title=f"tokens on [cyan]{d['chain']}[/cyan]", show_header=True, header_style="bold")
        table.add_column("symbol")
        table.add_column("address", style="dim")
        table.add_column("decimals", justify="right")
        table.add_column("source")
        for t in d["data"]["tokens"]:
            table.add_row(t["symbol"], t["address"],
                          "—" if t["decimals"] is None else str(t["decimals"]),
                          t["source"])
        stdout_console().print(table)

    emit(data, render)


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
        emit_error("not_found", command="token.remove", chain=cfg_name,
                   reason=f"no user token {sym} on {cfg_name}")
        raise typer.Exit(code=1)
    save_state(state)
    emit(
        {"ok": True, "command": "token.remove", "chain": cfg_name,
         "data": {"symbol": sym}},
        lambda d: stdout_console().print(f"removed {d['data']['symbol']} on {d['chain']}"),
    )

from __future__ import annotations

import json

import typer
from rich.panel import Panel
from rich.table import Table

from wallet.cli._output import emit, emit_error, info, stdout_console
from wallet.core import config as _config  # import-by-module so monkeypatch tracks
from wallet.core.config import _BUILTIN_PRESETS, get_chain
from wallet.storage.state import load_state

app = typer.Typer(no_args_is_help=True, help="Inspect available chain configurations")


def _user_chains() -> dict[str, dict]:
    p = _config.chains_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


@app.command("list")
def list_() -> None:
    """List every chain the wallet can talk to right now.

    Builtin presets and user-added entries from `~/.wallet/chains.json`
    are merged; user entries override builtins by name.
    """
    user = _user_chains()
    state = load_state()

    rows: list[dict] = []

    # Builtins (mark as user-override if user shadowed them)
    for name in _BUILTIN_PRESETS:
        cfg = get_chain(name)
        rows.append({
            "name": cfg.name,
            "chain_id": cfg.chain_id,
            "rpc_url": cfg.rpc_url,
            "source": "user-override" if name in user else "builtin",
            "default": cfg.name == state.default_chain,
        })

    # User-only entries (not in builtins)
    for name in user:
        if name in _BUILTIN_PRESETS:
            continue
        try:
            cfg = get_chain(name)
        except ValueError:
            continue
        rows.append({
            "name": cfg.name,
            "chain_id": cfg.chain_id,
            "rpc_url": cfg.rpc_url,
            "source": "user-added",
            "default": cfg.name == state.default_chain,
        })

    data = {
        "ok": True,
        "command": "chain.list",
        "data": {
            "chains": rows,
            "user_chains_file": str(_config.chains_config_path()),
            "default_chain": state.default_chain,
        },
    }

    def render(d):
        table = Table(show_header=True, header_style="bold")
        table.add_column("name")
        table.add_column("chain_id", justify="right")
        table.add_column("rpc_url", style="dim")
        table.add_column("source")
        table.add_column("default")
        for c in d["data"]["chains"]:
            table.add_row(
                c["name"],
                str(c["chain_id"]),
                c["rpc_url"],
                c["source"],
                "★" if c["default"] else "",
            )
        stdout_console().print(table)
        info(f"[dim]chains.json: {d['data']['user_chains_file']}[/dim]")

    emit(data, render)


@app.command("show")
def show(name: str = typer.Argument(..., help="Chain name (e.g. sepolia, ethereum)")) -> None:
    """Print the full ChainConfig for a chain, including protocol addresses."""
    try:
        cfg = get_chain(name)
    except ValueError as e:
        emit_error("not_found", command="chain.show", reason=str(e))
        raise typer.Exit(code=1)

    data = {
        "ok": True,
        "command": "chain.show",
        "chain": cfg.name,
        "data": cfg.model_dump(),
    }

    def render(d):
        stdout_console().print(Panel(
            json.dumps(d["data"], indent=2),
            title=f"chain config — [bold cyan]{d['chain']}[/bold cyan]",
            border_style="cyan",
        ))

    emit(data, render)

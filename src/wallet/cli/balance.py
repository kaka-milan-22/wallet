from __future__ import annotations

import typer
from rich.table import Table

from wallet.cli._common import make_web3_or_exit
from wallet.cli._output import emit, emit_error, stdout_console
from wallet.core.config import get_chain
from wallet.core.rpc import format_units
from wallet.core.tokens import balance_of, resolve_token
from wallet.storage.state import load_state


def _resolve_targets(state, account: str | None, all_watched: bool) -> list[tuple[str, str]]:
    if all_watched:
        rows: list[tuple[str, str]] = [(a.name, a.address) for a in state.accounts]
        rows += [(w.label or w.address[:10], w.address) for w in state.watch]
        return rows

    if account:
        a = state.find_account(account)
        if a:
            return [(a.name, a.address)]
        for w in state.watch:
            if w.label == account or w.address.lower() == account.lower():
                return [(w.label or w.address, w.address)]
        # Bare 0x address — one-shot lookup without needing prior `watch add`
        if account.startswith("0x") and len(account) == 42:
            from web3 import Web3
            try:
                addr = Web3.to_checksum_address(account)
                return [(account[:10] + "…", addr)]
            except ValueError:
                pass
        raise typer.BadParameter(f"no account or watched address: {account}")

    a = state.get_default_account()
    if not a:
        raise typer.BadParameter("no accounts registered — run `wallet account create <name>`")
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
    w3 = make_web3_or_exit(cfg, command="balance")

    try:
        targets = _resolve_targets(state, account, all_watched)
    except typer.BadParameter as e:
        emit_error("validation_error", command="balance", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    if token:
        try:
            info = resolve_token(w3, cfg, state, token)
        except ValueError as e:
            emit_error("not_found", command="balance", chain=cfg.name, reason=str(e))
            raise typer.Exit(code=2)
        unit = info.symbol
        decimals = info.decimals
        token_payload = {"symbol": info.symbol, "address": info.address, "decimals": info.decimals}

        def fetcher(addr: str) -> int:
            return balance_of(w3, info.address, addr)
    else:
        unit = cfg.native_symbol
        decimals = 18
        token_payload = None

        def fetcher(addr: str) -> int:
            return w3.eth.get_balance(w3.to_checksum_address(addr))

    balances: list[dict] = []
    for label, addr in targets:
        amt = fetcher(addr)
        balances.append({
            "label": label,
            "address": addr,
            "amount_wei": str(amt),
            "amount": format_units(amt, decimals),
        })

    data = {
        "ok": True,
        "command": "balance",
        "chain": cfg.name,
        "data": {
            "unit": unit,
            "decimals": decimals,
            "token": token_payload,
            "balances": balances,
        },
    }

    def render(d: dict) -> None:
        x = d["data"]
        title = x["unit"]
        if x["token"]:
            title = f"{x['unit']} [dim]({x['token']['address']})[/dim]"
        title += f" on [cyan]{d['chain']}[/cyan]"

        table = Table(title=title, show_header=True, header_style="bold")
        table.add_column("label")
        table.add_column("address", style="dim")
        table.add_column(f"balance ({x['unit']})", justify="right")
        for b in x["balances"]:
            table.add_row(b["label"], b["address"], b["amount"])
        stdout_console().print(table)

    emit(data, render)

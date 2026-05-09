from __future__ import annotations

from datetime import datetime, timezone

import typer
from rich.table import Table
from web3 import Web3

from wallet.cli._output import emit, emit_error, stdout_console
from wallet.core.config import get_chain
from wallet.core.rpc import format_units
from wallet.services.explorer import EtherscanError, list_native_txs, list_token_txs
from wallet.storage.state import load_state


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


def _ts(s: str) -> str:
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
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

    try:
        target_addr, target_label = _resolve_target(state, account, address)
    except typer.BadParameter as e:
        emit_error("validation_error", command="history", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        raw_txs = (
            list_token_txs(cfg, target_addr, limit=limit)
            if tokens
            else list_native_txs(cfg, target_addr, limit=limit)
        )
    except EtherscanError as e:
        emit_error("rpc_error", command="history", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=1)

    transactions: list[dict] = []
    for t in raw_txs:
        frm = t.get("from", "")
        to = t.get("to", "")
        target_low = target_addr.lower()
        direction = "out" if frm.lower() == target_low else "in" if to.lower() == target_low else "other"
        if tokens:
            decimals = int(t.get("tokenDecimal", "18"))
            symbol = t.get("tokenSymbol", "?")
            amount_wei = int(t.get("value", "0"))
            unit = symbol
            ok = True  # tokentx endpoint omits per-tx error
        else:
            decimals = 18
            amount_wei = int(t.get("value", "0"))
            unit = cfg.native_symbol
            ok = t.get("txreceipt_status", "1") == "1" and t.get("isError", "0") == "0"
        transactions.append({
            "ts": _ts(t.get("timeStamp", "")),
            "block_number": int(t.get("blockNumber", "0") or "0"),
            "direction": direction,
            "from": frm,
            "to": to,
            "amount_wei": str(amount_wei),
            "amount": format_units(amount_wei, decimals),
            "unit": unit,
            "decimals": decimals,
            "hash": t.get("hash", ""),
            "ok": ok,
        })

    data = {
        "ok": True,
        "command": "history",
        "chain": cfg.name,
        "data": {
            "address": target_addr,
            "label": target_label,
            "kind": "tokens" if tokens else "native",
            "limit": limit,
            "count": len(transactions),
            "transactions": transactions,
        },
    }

    def render(d: dict) -> None:
        x = d["data"]
        if not x["transactions"]:
            stdout_console().print(
                f"[dim]no transactions for {x['label']} ({x['address']})[/dim]"
            )
            return
        kind = "token transfers" if x["kind"] == "tokens" else "transactions"
        table = Table(
            title=f"{kind} for [bold]{x['label']}[/bold] ({x['address']}) on [cyan]{d['chain']}[/cyan]",
            show_header=True,
            header_style="bold",
        )
        table.add_column("when", style="dim")
        table.add_column("dir")
        table.add_column("counterparty", style="dim")
        table.add_column("amount", justify="right")
        table.add_column("status")
        table.add_column("hash", style="dim")
        for t in x["transactions"]:
            counter = t["to"] if t["direction"] == "out" else t["from"]
            counter_short = f"{counter[:10]}…{counter[-6:]}" if counter else "-"
            dir_label = (
                "[red]OUT[/red]" if t["direction"] == "out"
                else "[green]IN [/green]" if t["direction"] == "in"
                else "    "
            )
            status = "[green]ok[/green]" if t["ok"] else "[red]revert[/red]"
            table.add_row(
                t["ts"],
                dir_label,
                counter_short,
                f"{t['amount']} {t['unit']}",
                status,
                t["hash"][:12] + "…",
            )
        stdout_console().print(table)

    emit(data, render)

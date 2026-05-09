"""Helpers shared by sending commands (send, approve, revoke)."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.core.rpc import format_units
from wallet.core.signer import sign_transaction
from wallet.core.tx import PreparedTx, broadcast
from wallet.storage.state import WalletState

console = Console()


def resolve_address(state: WalletState, query: str) -> str:
    """Resolve `0x...`, `@alias`, or bare name to a checksummed address."""
    q = query.strip()
    if q.startswith("0x") and len(q) == 42:
        return Web3.to_checksum_address(q)

    needle = q[1:] if q.startswith("@") else q

    if needle in state.book:
        return Web3.to_checksum_address(state.book[needle])
    for a in state.accounts:
        if a.name == needle:
            return Web3.to_checksum_address(a.address)
    for w in state.watch:
        if w.label == needle:
            return Web3.to_checksum_address(w.address)
    raise typer.BadParameter(f"cannot resolve address: {query!r}")


def _label_for(state: WalletState, address: str) -> str:
    a = address.lower()
    for acc in state.accounts:
        if acc.address.lower() == a:
            return acc.name
    for alias, addr in state.book.items():
        if addr.lower() == a:
            return f"@{alias}"
    for w in state.watch:
        if w.address.lower() == a and w.label:
            return w.label
    return ""


def preview_tx(
    state: WalletState,
    chain: ChainConfig,
    prepared: PreparedTx,
) -> None:
    desc = prepared.description
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column()

    table.add_row("action", desc["kind"])
    table.add_row("chain", f"{chain.name} (chainId={chain.chain_id})")

    from_label = _label_for(state, desc["from"])
    table.add_row("from", f"{desc['from']}{' [' + from_label + ']' if from_label else ''}")

    if "to" in desc:
        to_label = _label_for(state, desc["to"])
        table.add_row("to", f"{desc['to']}{' [' + to_label + ']' if to_label else ''}")
    if "spender" in desc:
        sp_label = _label_for(state, desc["spender"])
        table.add_row("spender", f"{desc['spender']}{' [' + sp_label + ']' if sp_label else ''}")
    if "token_address" in desc:
        table.add_row("token", desc["token_address"])

    amount_str = format_units(desc["amount_wei"], desc["amount_decimals"])
    table.add_row("amount", f"{amount_str} {desc['amount_unit']}")

    tx = prepared.tx
    table.add_row("nonce", str(tx["nonce"]))
    table.add_row("gas limit", str(tx["gas"]))
    table.add_row(
        "max fee / gas",
        f"{format_units(tx['maxFeePerGas'], 9)} gwei",
    )
    table.add_row(
        "priority fee",
        f"{format_units(tx['maxPriorityFeePerGas'], 9)} gwei",
    )
    table.add_row(
        "est. fee",
        f"{format_units(prepared.estimated_fee_wei, 18)} {chain.native_symbol}",
    )

    console.print(Panel(table, title="transaction preview", border_style="cyan"))


def confirm_and_broadcast(
    w3,
    state: WalletState,
    chain: ChainConfig,
    sender_account,
    prepared: PreparedTx,
    *,
    dry_run: bool,
    yes: bool,
) -> None:
    preview_tx(state, chain, prepared)

    if dry_run:
        console.print("[dim]dry-run — not signing or broadcasting[/dim]")
        return

    if not yes and not Confirm.ask("send this transaction?", default=False):
        console.print("[yellow]aborted[/yellow]")
        raise typer.Exit(code=1)

    raw = sign_transaction(sender_account, prepared.tx)
    tx_hash = broadcast(w3, raw)
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    explorer_url = chain.explorer_tx_url.replace("{tx}", tx_hash)
    # print hash on its own line with soft_wrap so copy-paste captures the full
    # 66-char hash even on narrow terminals
    console.print("[green]submitted:[/green]")
    console.print(tx_hash, soft_wrap=True, highlight=False)
    console.print(explorer_url, soft_wrap=True, style="dim", highlight=False)

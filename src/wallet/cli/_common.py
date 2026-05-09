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


def _category(prepared: PreparedTx) -> str:
    kind = prepared.description.get("kind", "")
    if "approve" in kind:
        return "approve"
    if "transfer" in kind:
        return "send"
    return "unknown"


def _audit_event(
    prepared: PreparedTx,
    chain: ChainConfig,
    decision,
    caller: str,
    *,
    tx_hash: str | None,
    outcome: str,
    request_id: str | None,
) -> None:
    """Write one append-only audit log entry. Never raises (audit must not
    block the actual operation)."""
    from wallet.storage import audit

    desc = prepared.description
    pd = (
        f"{decision.severity}:{decision.reason}"
        if decision.severity != "allow"
        else "allow"
    )
    try:
        audit.write({
            "chain": chain.name,
            "from": desc.get("from"),
            "to": desc.get("to"),
            "spender": desc.get("spender"),
            "kind": _category(prepared),
            "amount_wei": str(desc.get("amount_wei", 0)),
            "unit": desc.get("amount_unit"),
            "token_address": desc.get("token_address"),
            "nonce": prepared.tx.get("nonce"),
            "gas": prepared.tx.get("gas"),
            "hash": tx_hash,
            "caller": caller,
            "request_id": request_id,
            "policy_decision": pd,
            "outcome": outcome,
        })
    except Exception:
        # never block the user's operation because of an audit failure
        pass


def confirm_and_broadcast(
    w3,
    state: WalletState,
    chain: ChainConfig,
    sender_account,
    prepared: PreparedTx,
    *,
    dry_run: bool,
    yes: bool,
    policy_bypass: bool = False,
    request_id: str | None = None,
) -> None:
    from wallet.cli._caller import caller_kind
    from wallet.core import policy as policy_mod
    from wallet.core.policy import Decision
    from wallet.storage import idempotency

    preview_tx(state, chain, prepared)

    if dry_run:
        console.print("[dim]dry-run — not signing or broadcasting[/dim]")
        return

    caller = caller_kind()

    # --- policy gate ---
    decision = policy_mod.evaluate(prepared, state, caller, bypass=policy_bypass)

    if not decision.allowed:
        _audit_event(
            prepared, chain, decision, caller,
            tx_hash=None, outcome="rejected", request_id=request_id,
        )
        console.print(f"[red]policy block:[/red] {decision.reason}")
        if "no-policy-configured" in decision.reason:
            console.print(
                "[dim]run `wallet policy init` in your terminal, then edit "
                "~/.wallet/policy.json to add allowlist entries.[/dim]"
            )
        raise typer.Exit(code=3)

    if decision.severity == "warn":
        if caller == "agent":
            _audit_event(
                prepared, chain, decision, caller,
                tx_hash=None, outcome="rejected", request_id=request_id,
            )
            console.print(f"[red]policy block (warn in agent mode):[/red] {decision.reason}")
            raise typer.Exit(code=3)
        console.print(f"[yellow]warn:[/yellow] {decision.reason}")
        if not yes and not Confirm.ask("proceed despite warning?", default=False):
            _audit_event(
                prepared, chain, decision, caller,
                tx_hash=None, outcome="user_aborted_after_warn", request_id=request_id,
            )
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(code=1)

    # --- idempotency check ---
    fp = idempotency.fingerprint(prepared, chain)
    if request_id is not None:
        try:
            cached = idempotency.lookup(request_id, fp)
        except idempotency.IdempotencyMismatch as e:
            block = Decision(allowed=False, reason=f"idempotency-mismatch", severity="block")
            _audit_event(
                prepared, chain, block, caller,
                tx_hash=None, outcome="rejected", request_id=request_id,
            )
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=3) from None
        if cached is not None:
            _audit_event(
                prepared, chain, decision, caller,
                tx_hash=cached.tx_hash, outcome="replayed_idempotent", request_id=request_id,
            )
            console.print(f"[dim]idempotent replay (request_id={request_id}, original from {cached.created_at})[/dim]")
            if cached.tx_hash:
                console.print(cached.tx_hash, soft_wrap=True, highlight=False)
                console.print(
                    chain.explorer_tx_url.replace("{tx}", cached.tx_hash),
                    soft_wrap=True, style="dim", highlight=False,
                )
            return
    elif caller == "agent":
        # Agents must always provide --request-id for broadcast; without it,
        # a transient retry would double-spend.
        block = Decision(allowed=False, reason="missing-request-id-for-agent", severity="block")
        _audit_event(
            prepared, chain, block, caller,
            tx_hash=None, outcome="rejected", request_id=None,
        )
        console.print(
            "[red]policy block:[/red] agent broadcast requires --request-id\n"
            "[dim]generate one via `python -c 'import uuid; print(uuid.uuid4())'`[/dim]"
        )
        raise typer.Exit(code=3)

    # --- final user confirm prompt ---
    if not yes and not Confirm.ask("send this transaction?", default=False):
        _audit_event(
            prepared, chain, decision, caller,
            tx_hash=None, outcome="user_aborted", request_id=request_id,
        )
        console.print("[yellow]aborted[/yellow]")
        raise typer.Exit(code=1)

    # --- sign + broadcast ---
    raw = sign_transaction(sender_account, prepared.tx)
    tx_hash = broadcast(w3, raw)
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    if request_id is not None:
        try:
            idempotency.record(
                request_id, fp,
                tx_hash=tx_hash,
                nonce=prepared.tx.get("nonce"),
                outcome="broadcast",
            )
        except Exception:
            pass  # never let idempotency persist failure block the broadcast result

    _audit_event(
        prepared, chain, decision, caller,
        tx_hash=tx_hash, outcome="broadcast", request_id=request_id,
    )

    explorer_url = chain.explorer_tx_url.replace("{tx}", tx_hash)
    console.print("[green]submitted:[/green]")
    console.print(tx_hash, soft_wrap=True, highlight=False)
    console.print(explorer_url, soft_wrap=True, style="dim", highlight=False)

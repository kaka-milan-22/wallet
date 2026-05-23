"""Stuck-tx recovery: `wallet tx pending / cancel / replace <nonce>`.

When a tx is sitting in mempool because base-fee spiked above its
`maxFeePerGas`, these commands let the operator (or agent, with --request-id)
either drop a 0-value self-send into that nonce slot (cancel) or rebroadcast
the original calldata at higher gas (replace). Both go through the standard
`confirm_and_broadcast` pipeline — policy / idempotency / audit gate every
replacement just like a fresh tx.
"""

from __future__ import annotations

import typer
from rich.table import Table

from wallet.cli._common import (
    confirm_and_broadcast,
    make_web3_or_exit,
    resolve_account,
)
from wallet.cli._output import emit, emit_error, stdout_console
from wallet.core.config import get_chain
from wallet.core.tx_replace import (
    StuckTxError,
    list_pending,
    prepare_cancel,
    prepare_replacement,
)
from wallet.storage.state import load_state

app = typer.Typer(no_args_is_help=True, help="Stuck-tx recovery (cancel / speedup)")


@app.command("pending")
def pending(
    account: str | None = typer.Option(None, "--account", "-a", help="Account to scan (default: current default)"),
    chain: str | None = typer.Option(None, "--chain", help="Chain name override"),
) -> None:
    """List broadcasts cached locally that have no receipt on chain yet.

    Source is `~/.wallet/idempotency.json`; entries older than 24h are swept.
    Mined txs (receipt.blockNumber set) are excluded.
    """
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="tx.pending")
    sender = resolve_account(state, account)

    items = list_pending(w3, sender.address)

    def explorer(tx_hash: str) -> str:
        return cfg.explorer_tx_url.replace("{tx}", tx_hash)

    payload = {
        "ok": True,
        "command": "tx.pending",
        "chain": cfg.name,
        "data": {
            "account": sender.address,
            "count": len(items),
            "pending": [
                {
                    "nonce": p.nonce,
                    "tx_hash": p.tx_hash,
                    "request_id": p.request_id,
                    "kind": p.kind,
                    "created_at": p.created_at,
                    "explorer_url": explorer(p.tx_hash),
                }
                for p in items
            ],
        },
    }

    def render(_d):
        if not items:
            stdout_console().print("[dim]no pending broadcasts[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("nonce", justify="right")
        table.add_column("kind")
        table.add_column("request_id")
        table.add_column("submitted")
        table.add_column("tx_hash")
        for p in items:
            table.add_row(
                str(p.nonce),
                p.kind,
                p.request_id,
                p.created_at,
                p.tx_hash,
            )
        stdout_console().print(table)

    emit(payload, render)


@app.command("cancel")
def cancel(
    nonce: int = typer.Argument(..., help="Nonce of the stuck tx to cancel"),
    account: str | None = typer.Option(None, "--account", "-a", help="Account that owns the stuck tx"),
    chain: str | None = typer.Option(None, "--chain", help="Chain name override"),
    speedup_pct: int = typer.Option(25, "--speedup-pct", help="Extra gas bump above the 110% replacement floor"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run", help="Default is dry-run; pass --broadcast to submit"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass", help="Skip policy gate (TTY-only)"),
    request_id: str | None = typer.Option(None, "--request-id", help="Idempotency key. Required for agent broadcast."),
    wait: bool = typer.Option(False, "--wait", help="Block until tx receipt; merges block/gas/fee into envelope. Exit 5 on revert."),
    wait_timeout: int = typer.Option(60, "--wait-timeout", envvar="WALLET_WAIT_TIMEOUT", help="Seconds to wait for receipt (default 60)."),
) -> None:
    """Drop a 0-value self-send into <nonce> to free a stuck mempool slot."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="tx.cancel")
    sender = resolve_account(state, account)

    prepared = prepare_cancel(w3, cfg, sender.address, nonce, speedup_pct=speedup_pct)

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast,
        yes=yes,
        policy_bypass=policy_bypass,
        request_id=request_id,
        preserve_nonce=True,
        wait=wait,
        wait_timeout=wait_timeout,
    )


@app.command("replace")
def replace(
    nonce: int = typer.Argument(..., help="Nonce of the stuck tx to speed up"),
    account: str | None = typer.Option(None, "--account", "-a", help="Account that owns the stuck tx"),
    chain: str | None = typer.Option(None, "--chain", help="Chain name override"),
    speedup_pct: int = typer.Option(25, "--speedup-pct", help="Extra gas bump above the 110% replacement floor"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run", help="Default is dry-run; pass --broadcast to submit"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass", help="Skip policy gate (TTY-only)"),
    request_id: str | None = typer.Option(None, "--request-id", help="Idempotency key. Required for agent broadcast."),
    wait: bool = typer.Option(False, "--wait", help="Block until tx receipt; merges block/gas/fee into envelope. Exit 5 on revert."),
    wait_timeout: int = typer.Option(60, "--wait-timeout", envvar="WALLET_WAIT_TIMEOUT", help="Seconds to wait for receipt (default 60)."),
) -> None:
    """Re-broadcast the original tx at <nonce> with higher gas."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="tx.replace")
    sender = resolve_account(state, account)

    try:
        prepared = prepare_replacement(
            w3, cfg, sender.address, nonce, speedup_pct=speedup_pct,
        )
    except StuckTxError as e:
        emit_error(
            "not_found",
            command="tx.replace",
            chain=cfg.name,
            reason=str(e),
        )
        raise typer.Exit(code=1) from None

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast,
        yes=yes,
        policy_bypass=policy_bypass,
        request_id=request_id,
        preserve_nonce=True,
        wait=wait,
        wait_timeout=wait_timeout,
    )

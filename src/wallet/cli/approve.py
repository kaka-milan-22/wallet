from __future__ import annotations

import typer
from rich.table import Table

from wallet.cli._common import (
    confirm_and_broadcast,
    make_web3_or_exit,
    resolve_account,
    resolve_address,
)
from wallet.cli._output import emit, emit_error, stdout_console
from wallet.core.config import get_chain
from wallet.core.rpc import format_units, parse_units
from wallet.core.tokens import MAX_UINT256, allowance as get_allowance, resolve_token
from wallet.core.tx import prepare_erc20_approve
from wallet.storage.state import load_state

app = typer.Typer(no_args_is_help=True, help="ERC-20 approval management")


@app.command("set")
def set_allowance(
    token: str = typer.Argument(..., help="Token symbol or 0x address"),
    spender: str = typer.Argument(..., help="Spender: 0x address, @alias, or name"),
    amount: str = typer.Argument(None, help="Amount in human units (omit if --unlimited)"),
    unlimited: bool = typer.Option(False, "--unlimited", help="Approve max uint256"),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass", help="Skip policy gate (TTY-only)"),
    request_id: str | None = typer.Option(None, "--request-id", help="Idempotency key (uuid). Required for non-TTY broadcast."),
) -> None:
    """Approve a spender to move tokens on your behalf."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="approve")

    try:
        sender = resolve_account(state, account)
        spender_addr = resolve_address(state, spender)
    except typer.BadParameter as e:
        emit_error("validation_error", command="approve", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        info = resolve_token(w3, cfg, state, token)
    except ValueError as e:
        emit_error("not_found", command="approve", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    if unlimited:
        if amount is not None:
            emit_error("validation_error", command="approve", chain=cfg.name,
                       reason="cannot pass both an amount and --unlimited")
            raise typer.Exit(code=2)
        amount_raw = MAX_UINT256
    else:
        if amount is None:
            emit_error("validation_error", command="approve", chain=cfg.name,
                       reason="amount required (or use --unlimited)")
            raise typer.Exit(code=2)
        amount_raw = parse_units(amount, info.decimals)

    prepared = prepare_erc20_approve(
        w3, cfg, sender.address, info.address, spender_addr, amount_raw,
        info.symbol, info.decimals,
    )
    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast, yes=yes, policy_bypass=policy_bypass,
        request_id=request_id,
    )


@app.command("show")
def show(
    token: str = typer.Argument(..., help="Token symbol or 0x address"),
    owner: str | None = typer.Option(None, "--owner", help="Default: current account"),
    spender: str | None = typer.Option(None, "--spender", help="Spender to check (required)"),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    """Show how much a spender is allowed to move on the owner's behalf."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)

    if not spender:
        emit_error("validation_error", command="approve.show", chain=cfg.name,
                   reason="--spender is required")
        raise typer.Exit(code=2)

    w3 = make_web3_or_exit(cfg, command="approve")

    try:
        info_ = resolve_token(w3, cfg, state, token)
    except ValueError as e:
        emit_error("not_found", command="approve.show", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        owner_addr = resolve_address(state, owner) if owner else resolve_account(state, None).address
        spender_addr = resolve_address(state, spender)
    except typer.BadParameter as e:
        emit_error("validation_error", command="approve.show", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    raw = get_allowance(w3, info_.address, owner_addr, spender_addr)
    is_max = raw == MAX_UINT256

    data = {
        "ok": True,
        "command": "approve.show",
        "chain": cfg.name,
        "data": {
            "token": {"symbol": info_.symbol, "address": info_.address, "decimals": info_.decimals},
            "owner": owner_addr,
            "spender": spender_addr,
            "allowance_wei": str(raw),
            "allowance": format_units(raw, info_.decimals),
            "is_unlimited": is_max,
        },
    }

    def render(d):
        x = d["data"]
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column()
        table.add_row("token", f"{x['token']['symbol']} ({x['token']['address']})")
        table.add_row("owner", x["owner"])
        table.add_row("spender", x["spender"])
        if x["is_unlimited"]:
            table.add_row("allowance", "[red]UNLIMITED (max uint256)[/red]")
        else:
            table.add_row("allowance", f"{x['allowance']} {x['token']['symbol']}")
        stdout_console().print(table)

    emit(data, render)


@app.command("revoke")
def revoke(
    token: str = typer.Argument(..., help="Token symbol or 0x address"),
    spender: str = typer.Argument(..., help="Spender to revoke"),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass", help="Skip policy gate (TTY-only)"),
    request_id: str | None = typer.Option(None, "--request-id", help="Idempotency key (uuid). Required for non-TTY broadcast."),
) -> None:
    """Revoke a spender's allowance (sets it to 0)."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="approve")

    try:
        sender = resolve_account(state, account)
        spender_addr = resolve_address(state, spender)
    except typer.BadParameter as e:
        emit_error("validation_error", command="revoke", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        info = resolve_token(w3, cfg, state, token)
    except ValueError as e:
        emit_error("not_found", command="revoke", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    prepared = prepare_erc20_approve(
        w3, cfg, sender.address, info.address, spender_addr, 0,
        info.symbol, info.decimals,
    )
    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast, yes=yes, policy_bypass=policy_bypass,
        request_id=request_id,
    )

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from wallet.cli._common import confirm_and_broadcast, resolve_address
from wallet.core.config import get_chain
from wallet.core.rpc import format_units, make_web3, parse_units
from wallet.core.tokens import MAX_UINT256, allowance as get_allowance, resolve_token
from wallet.core.tx import prepare_erc20_approve
from wallet.storage.state import load_state

app = typer.Typer(no_args_is_help=True, help="ERC-20 approval management")
console = Console()


def _sender(state, account: str | None):
    if account:
        a = state.find_account(account)
        if not a:
            raise typer.BadParameter(f"unknown account: {account}")
        return a
    a = state.get_default_account()
    if not a:
        raise typer.BadParameter("no default account")
    return a


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
) -> None:
    """Approve a spender to move tokens on your behalf."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)

    sender = _sender(state, account)
    spender_addr = resolve_address(state, spender)
    info = resolve_token(w3, cfg, state, token)

    if unlimited:
        if amount is not None:
            raise typer.BadParameter("cannot pass both an amount and --unlimited")
        amount_raw = MAX_UINT256
    else:
        if amount is None:
            raise typer.BadParameter("amount required (or use --unlimited)")
        amount_raw = parse_units(amount, info.decimals)

    prepared = prepare_erc20_approve(
        w3, cfg, sender.address, info.address, spender_addr, amount_raw,
        info.symbol, info.decimals,
    )
    confirm_and_broadcast(w3, state, cfg, sender, prepared, dry_run=not broadcast, yes=yes)


@app.command("show")
def show(
    token: str = typer.Argument(..., help="Token symbol or 0x address"),
    owner: str | None = typer.Option(None, "--owner", help="Default: current account"),
    spender: str | None = typer.Option(None, "--spender", help="Spender to check (required)"),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    """Show how much a spender is allowed to move on the owner's behalf."""
    if not spender:
        raise typer.BadParameter("--spender is required")

    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)

    info = resolve_token(w3, cfg, state, token)
    owner_addr = resolve_address(state, owner) if owner else _sender(state, None).address
    spender_addr = resolve_address(state, spender)

    raw = get_allowance(w3, info.address, owner_addr, spender_addr)
    is_max = raw == MAX_UINT256

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("token", f"{info.symbol} ({info.address})")
    table.add_row("owner", owner_addr)
    table.add_row("spender", spender_addr)
    table.add_row(
        "allowance",
        "[red]UNLIMITED (max uint256)[/red]" if is_max else f"{format_units(raw, info.decimals)} {info.symbol}",
    )
    console.print(table)


@app.command("revoke")
def revoke(
    token: str = typer.Argument(..., help="Token symbol or 0x address"),
    spender: str = typer.Argument(..., help="Spender to revoke"),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Revoke a spender's allowance (sets it to 0)."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)

    sender = _sender(state, account)
    spender_addr = resolve_address(state, spender)
    info = resolve_token(w3, cfg, state, token)

    prepared = prepare_erc20_approve(
        w3, cfg, sender.address, info.address, spender_addr, 0,
        info.symbol, info.decimals,
    )
    confirm_and_broadcast(w3, state, cfg, sender, prepared, dry_run=not broadcast, yes=yes)

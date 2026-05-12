from __future__ import annotations

import typer
from rich.table import Table

from wallet.cli._output import emit, emit_error, info, stdout_console
from wallet.core.config import get_chain
from wallet.core.rpc import format_units, make_web3
from wallet.cli._common import confirm_and_broadcast
from wallet.core.rpc import parse_units
from wallet.protocols.aave import (
    WITHDRAW_MAX_AMOUNT,
    base_to_usd,
    get_account_summary,
    get_all_rates,
    get_all_reserves,
    get_user_positions,
    prepare_supply,
    prepare_withdraw,
    ray_to_pct,
    resolve_aave_reserve,
)
from wallet.protocols.swap import InsufficientAllowance
from wallet.storage.state import load_state

app = typer.Typer(no_args_is_help=True, help="Aave V3 read-only views (positions, rates)")


def _resolve_account(state, account: str | None) -> tuple[str, str]:
    if account:
        a = state.find_account(account)
        if a:
            return a.name, a.address
        for w in state.watch:
            if w.label == account or w.address.lower() == account.lower():
                return (w.label or w.address[:10]), w.address
        raise typer.BadParameter(f"no account or watched address: {account}")
    a = state.get_default_account()
    if not a:
        raise typer.BadParameter("no default account; pass --account")
    return a.name, a.address


@app.command("positions")
def positions(
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    """Show your Aave V3 supplies / borrows + health factor."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)

    try:
        label, address = _resolve_account(state, account)
    except typer.BadParameter as e:
        emit_error("validation_error", command="aave.positions", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    summary = get_account_summary(w3, cfg, address)
    reserves = get_all_reserves(w3, cfg)
    user_positions = get_user_positions(w3, cfg, address, reserves=reserves)

    supplies = [
        {
            "symbol": p.reserve.symbol,
            "asset_address": p.reserve.asset_address,
            "decimals": p.reserve.decimals,
            "amount_wei": str(p.supplied_wei),
            "amount": format_units(p.supplied_wei, p.reserve.decimals),
        }
        for p in user_positions if p.supplied_wei > 0
    ]
    borrows = [
        {
            "symbol": p.reserve.symbol,
            "asset_address": p.reserve.asset_address,
            "decimals": p.reserve.decimals,
            "amount_wei": str(p.variable_debt_wei),
            "amount": format_units(p.variable_debt_wei, p.reserve.decimals),
        }
        for p in user_positions if p.variable_debt_wei > 0
    ]

    data = {
        "ok": True,
        "command": "aave.positions",
        "chain": cfg.name,
        "data": {
            "account": {"label": label, "address": address},
            "summary": {
                "total_collateral_base_wei": str(summary.total_collateral_base_wei),
                "total_collateral_usd": base_to_usd(summary.total_collateral_base_wei),
                "total_debt_base_wei": str(summary.total_debt_base_wei),
                "total_debt_usd": base_to_usd(summary.total_debt_base_wei),
                "available_borrows_base_wei": str(summary.available_borrows_base_wei),
                "available_borrows_usd": base_to_usd(summary.available_borrows_base_wei),
                "ltv_bps": summary.ltv_bps,
                "liquidation_threshold_bps": summary.liquidation_threshold_bps,
                "health_factor": summary.health_factor,  # float or null
            },
            "supplies": supplies,
            "borrows": borrows,
        },
    }

    def render(d):
        x = d["data"]
        c = stdout_console()
        s = x["summary"]

        hf = s["health_factor"]
        if hf is None:
            hf_display = "[green]∞ (no debt)[/green]"
        elif hf < 1.1:
            hf_display = f"[red]{hf:.3f}[/red]"
        elif hf < 1.5:
            hf_display = f"[yellow]{hf:.3f}[/yellow]"
        else:
            hf_display = f"[green]{hf:.3f}[/green]"

        sumtab = Table(show_header=False, box=None, padding=(0, 2),
                       title=f"Aave V3 — [bold]{x['account']['label']}[/bold] on [cyan]{d['chain']}[/cyan]")
        sumtab.add_column(style="bold cyan")
        sumtab.add_column()
        sumtab.add_row("collateral", f"${s['total_collateral_usd']}")
        sumtab.add_row("debt", f"${s['total_debt_usd']}")
        sumtab.add_row("available borrows", f"${s['available_borrows_usd']}")
        sumtab.add_row("max LTV", f"{s['ltv_bps'] / 100:.2f}%")
        sumtab.add_row("liq. threshold", f"{s['liquidation_threshold_bps'] / 100:.2f}%")
        sumtab.add_row("health factor", hf_display)
        c.print(sumtab)

        if x["supplies"]:
            t = Table(show_header=True, header_style="bold", title="supplies")
            t.add_column("symbol")
            t.add_column("amount", justify="right")
            t.add_column("asset", style="dim")
            for row in x["supplies"]:
                t.add_row(row["symbol"], row["amount"], row["asset_address"])
            c.print(t)
        else:
            info("[dim]no supplies[/dim]")

        if x["borrows"]:
            t = Table(show_header=True, header_style="bold", title="borrows (variable)")
            t.add_column("symbol")
            t.add_column("amount", justify="right")
            t.add_column("asset", style="dim")
            for row in x["borrows"]:
                t.add_row(row["symbol"], row["amount"], row["asset_address"])
            c.print(t)
        else:
            info("[dim]no borrows[/dim]")

    emit(data, render)


@app.command("rates")
def rates(
    token: str | None = typer.Option(None, "--token", "-t", help="Filter to a single symbol"),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    """Show current supply / borrow APRs for every reserve."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)

    reserves = get_all_reserves(w3, cfg)
    if token:
        token_up = token.upper()
        reserves = [r for r in reserves if r.symbol.upper() == token_up]
        if not reserves:
            emit_error("not_found", command="aave.rates", chain=cfg.name,
                       reason=f"no Aave reserve with symbol {token!r}")
            raise typer.Exit(code=2)

    all_rates = get_all_rates(w3, cfg, reserves=reserves)
    rates_data = [
        {
            "symbol": r.reserve.symbol,
            "asset_address": r.reserve.asset_address,
            "decimals": r.reserve.decimals,
            "supply_apr_ray": str(r.supply_apr_ray),
            "supply_apr_pct": ray_to_pct(r.supply_apr_ray),
            "variable_borrow_apr_ray": str(r.variable_borrow_apr_ray),
            "variable_borrow_apr_pct": ray_to_pct(r.variable_borrow_apr_ray),
        }
        for r in all_rates
    ]

    data = {
        "ok": True,
        "command": "aave.rates",
        "chain": cfg.name,
        "data": {"rates": rates_data},
    }

    def render(d):
        t = Table(show_header=True, header_style="bold",
                  title=f"Aave V3 rates on [cyan]{d['chain']}[/cyan]")
        t.add_column("symbol")
        t.add_column("supply APR", justify="right", style="green")
        t.add_column("borrow APR", justify="right", style="red")
        t.add_column("asset", style="dim")
        for row in d["data"]["rates"]:
            t.add_row(
                row["symbol"],
                f"{row['supply_apr_pct']}%",
                f"{row['variable_borrow_apr_pct']}%",
                row["asset_address"],
            )
        stdout_console().print(t)

    emit(data, render)


@app.command("supply")
def supply(
    token: str = typer.Argument(..., help="Aave reserve symbol (e.g. USDC, WETH) or 0x address"),
    amount: str = typer.Argument(..., help="Amount in human units (e.g. 100)"),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass", help="Skip policy gate (TTY-only)"),
    request_id: str | None = typer.Option(None, "--request-id", help="Idempotency key. Required for non-TTY broadcast."),
) -> None:
    """Supply tokens to an Aave V3 reserve (earn supply APR)."""
    from wallet.cli._output import emit_error as _emit_err
    from wallet.core.config import get_chain
    from wallet.core.rpc import format_units, make_web3

    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)

    if account:
        sender = state.find_account(account)
        if not sender:
            _emit_err("validation_error", command="aave.supply", chain=cfg.name,
                      reason=f"unknown account: {account}")
            raise typer.Exit(code=2)
    else:
        sender = state.get_default_account()
        if not sender:
            _emit_err("validation_error", command="aave.supply", chain=cfg.name,
                      reason="no default account; pass --account")
            raise typer.Exit(code=2)

    try:
        reserve = resolve_aave_reserve(w3, cfg, token)
    except ValueError as e:
        _emit_err("not_found", command="aave.supply", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        amount_wei = parse_units(amount, reserve.decimals)
    except ValueError as e:
        _emit_err("validation_error", command="aave.supply", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        prepared = prepare_supply(w3, cfg, sender.address, reserve, amount_wei)
    except InsufficientAllowance as e:
        _emit_err(
            "insufficient_allowance",
            command="aave.supply", chain=cfg.name,
            reason=str(e),
            data={
                "token_symbol": e.token_symbol,
                "token_address": e.token_address,
                "spender": e.spender,
                "current_wei": str(e.current_wei),
                "current": format_units(e.current_wei, reserve.decimals),
                "required_wei": str(e.required_wei),
                "required": format_units(e.required_wei, reserve.decimals),
                "suggested_command": (
                    f"wallet approve set {e.token_symbol} {e.spender} "
                    f"{format_units(e.required_wei, reserve.decimals)}"
                ),
            },
        )
        raise typer.Exit(code=2)

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast, yes=yes,
        policy_bypass=policy_bypass, request_id=request_id,
    )


@app.command("withdraw")
def withdraw(
    token: str = typer.Argument(..., help="Aave reserve symbol or 0x address"),
    amount: str = typer.Argument(None, help="Amount in human units (omit if --max)"),
    max_: bool = typer.Option(False, "--max", help="Withdraw the entire aToken balance"),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass", help="Skip policy gate (TTY-only)"),
    request_id: str | None = typer.Option(None, "--request-id", help="Idempotency key. Required for non-TTY broadcast."),
) -> None:
    """Withdraw tokens from an Aave V3 reserve.

    Aave reverts if the post-withdraw health factor would drop below 1.0.
    """
    from wallet.cli._output import emit_error as _emit_err
    from wallet.core.config import get_chain
    from wallet.core.rpc import make_web3

    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3(cfg)

    if account:
        sender = state.find_account(account)
        if not sender:
            _emit_err("validation_error", command="aave.withdraw", chain=cfg.name,
                      reason=f"unknown account: {account}")
            raise typer.Exit(code=2)
    else:
        sender = state.get_default_account()
        if not sender:
            _emit_err("validation_error", command="aave.withdraw", chain=cfg.name,
                      reason="no default account; pass --account")
            raise typer.Exit(code=2)

    try:
        reserve = resolve_aave_reserve(w3, cfg, token)
    except ValueError as e:
        _emit_err("not_found", command="aave.withdraw", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    if max_:
        if amount is not None:
            _emit_err("validation_error", command="aave.withdraw", chain=cfg.name,
                      reason="cannot pass both --max and an amount")
            raise typer.Exit(code=2)
        amount_wei = WITHDRAW_MAX_AMOUNT
    else:
        if amount is None:
            _emit_err("validation_error", command="aave.withdraw", chain=cfg.name,
                      reason="amount required (or use --max)")
            raise typer.Exit(code=2)
        try:
            amount_wei = parse_units(amount, reserve.decimals)
        except ValueError as e:
            _emit_err("validation_error", command="aave.withdraw", chain=cfg.name, reason=str(e))
            raise typer.Exit(code=2)

    prepared = prepare_withdraw(w3, cfg, sender.address, reserve, amount_wei)

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast, yes=yes,
        policy_bypass=policy_bypass, request_id=request_id,
    )

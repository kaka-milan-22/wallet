"""`wallet lp` — Uniswap V3 LP primitives (positions / collect / remove / mint / increase).

Each write command builds a `PreparedTx` via `protocols.uniswap_v3_lp` and
hands it to `confirm_and_broadcast`, so policy / idempotency / audit
guardrails apply uniformly. Re-range is intentionally NOT a single command:
agents compose `remove` → `collect` → `swap` → `mint` themselves so each
on-chain side-effect passes through the gate independently.
"""

from __future__ import annotations

import typer
from rich.table import Table
from web3.exceptions import ContractLogicError as _ContractLogicError

from wallet.cli._common import (
    confirm_and_broadcast,
    make_web3_or_exit,
    resolve_account,
)
from wallet.cli._output import emit, emit_error, info, stdout_console
from wallet.cli.swap import _resolve_token_or_native
from wallet.core.config import get_chain
from wallet.core.rpc import format_units, parse_units
from wallet.protocols.swap import InsufficientAllowance
from wallet.protocols.uniswap_v3_lp import (
    get_positions,
    prepare_collect,
    prepare_decrease_liquidity,
    prepare_increase_liquidity,
    prepare_mint,
)
from wallet.storage.state import load_state


app = typer.Typer(no_args_is_help=True, help="Uniswap V3 LP position management")


def _format_revert(exc) -> str:
    return str(exc)


@app.command("positions")
def positions(
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
) -> None:
    """List your Uniswap V3 LP positions on NFPM with in-range status + amounts."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="lp.positions")

    try:
        sender = resolve_account(state, account)
    except typer.BadParameter as e:
        emit_error("validation_error", command="lp.positions", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        items = get_positions(w3, cfg, sender.address)
    except ValueError as e:
        emit_error("not_found", command="lp.positions", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    rows = [
        {
            "token_id": p.token_id,
            "pair": f"{p.token0_symbol}/{p.token1_symbol}",
            "fee": p.fee,
            "tick_lower": p.tick_lower,
            "tick_upper": p.tick_upper,
            "current_tick": p.current_tick,
            "in_range": p.in_range,
            "liquidity_wei": str(p.liquidity),
            "amount0_wei": str(p.amount0_wei),
            "amount0": format_units(p.amount0_wei, p.token0_decimals),
            "amount1_wei": str(p.amount1_wei),
            "amount1": format_units(p.amount1_wei, p.token1_decimals),
            "tokens_owed0_wei": str(p.tokens_owed0),
            "tokens_owed0": format_units(p.tokens_owed0, p.token0_decimals),
            "tokens_owed1_wei": str(p.tokens_owed1),
            "tokens_owed1": format_units(p.tokens_owed1, p.token1_decimals),
            "token0_address": p.token0_address,
            "token1_address": p.token1_address,
            "token0_symbol": p.token0_symbol,
            "token1_symbol": p.token1_symbol,
            "token0_decimals": p.token0_decimals,
            "token1_decimals": p.token1_decimals,
            "pool_address": p.pool_address,
            "sqrt_price_x96": str(p.current_sqrt_price_x96),
        }
        for p in items
    ]

    data = {
        "ok": True,
        "command": "lp.positions",
        "chain": cfg.name,
        "data": {
            "account": {"address": sender.address, "label": sender.name},
            "positions": rows,
        },
    }

    def render(d):
        if not d["data"]["positions"]:
            info("[dim]no LP positions[/dim]")
            return
        t = Table(
            show_header=True, header_style="bold",
            title=f"Uniswap V3 LP — [bold]{d['data']['account']['label']}[/bold] on [cyan]{d['chain']}[/cyan]",
        )
        t.add_column("id")
        t.add_column("pair")
        t.add_column("fee bps")
        t.add_column("range (tick)")
        t.add_column("current tick")
        t.add_column("status")
        t.add_column("amount0", justify="right")
        t.add_column("amount1", justify="right")
        t.add_column("fees owed (t0/t1)", justify="right")
        for row in d["data"]["positions"]:
            status = "[green]in range[/green]" if row["in_range"] else "[yellow]out of range[/yellow]"
            t.add_row(
                str(row["token_id"]),
                row["pair"],
                str(row["fee"]),
                f"[{row['tick_lower']}, {row['tick_upper']})",
                str(row["current_tick"]),
                status,
                f"{row['amount0']} {row['token0_symbol']}",
                f"{row['amount1']} {row['token1_symbol']}",
                f"{row['tokens_owed0']} / {row['tokens_owed1']}",
            )
        stdout_console().print(t)

    emit(data, render)


@app.command("collect")
def collect(
    token_id: int = typer.Argument(..., help="NFT tokenId from `wallet lp positions`"),
    recipient: str | None = typer.Option(
        None, "--recipient", help="Address to receive fees (default: sender)"
    ),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass"),
    request_id: str | None = typer.Option(None, "--request-id"),
) -> None:
    """Sweep accrued fees + already-decreased liquidity to `recipient` (default sender)."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="lp.collect")

    try:
        sender = resolve_account(state, account)
    except typer.BadParameter as e:
        emit_error("validation_error", command="lp.collect", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        prepared = prepare_collect(w3, cfg, sender.address, token_id, recipient=recipient)
    except _ContractLogicError as e:
        emit_error(
            "simulation_reverted",
            command="lp.collect", chain=cfg.name,
            reason=_format_revert(e),
        )
        raise typer.Exit(code=3)
    except RuntimeError as e:
        emit_error(
            "simulation_reverted",
            command="lp.collect", chain=cfg.name,
            reason=str(e),
        )
        raise typer.Exit(code=3)
    except ValueError as e:
        emit_error("validation_error", command="lp.collect", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast, yes=yes,
        policy_bypass=policy_bypass, request_id=request_id,
    )


@app.command("remove")
def remove(
    token_id: int = typer.Argument(..., help="NFT tokenId"),
    percent: float = typer.Option(
        ..., "--percent", help="Percent of liquidity to burn (e.g. 100 for all)"
    ),
    slippage_bps: int = typer.Option(50, "--slippage-bps", help="Slippage tolerance (default 50 = 0.5%)"),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass"),
    request_id: str | None = typer.Option(None, "--request-id"),
) -> None:
    """Burn `--percent`% of a position's liquidity.

    The proceeds land in NFPM's owed-fees buckets — run `wallet lp collect`
    after this to sweep them to your wallet.
    """
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="lp.remove")

    try:
        sender = resolve_account(state, account)
    except typer.BadParameter as e:
        emit_error("validation_error", command="lp.remove", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        prepared = prepare_decrease_liquidity(
            w3, cfg, sender.address, token_id, percent, slippage_bps,
        )
    except _ContractLogicError as e:
        emit_error(
            "simulation_reverted",
            command="lp.remove", chain=cfg.name,
            reason=_format_revert(e),
        )
        raise typer.Exit(code=3)
    except RuntimeError as e:
        emit_error(
            "simulation_reverted",
            command="lp.remove", chain=cfg.name,
            reason=str(e),
        )
        raise typer.Exit(code=3)
    except ValueError as e:
        emit_error("validation_error", command="lp.remove", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast, yes=yes,
        policy_bypass=policy_bypass, request_id=request_id,
    )


def _allowance_error_envelope(
    *, command: str, chain_name: str, exc: InsufficientAllowance, decimals: int
):
    emit_error(
        "insufficient_allowance",
        command=command, chain=chain_name,
        reason=str(exc),
        data={
            "token_symbol": exc.token_symbol,
            "token_address": exc.token_address,
            "spender": exc.spender,
            "current_wei": str(exc.current_wei),
            "current": format_units(exc.current_wei, decimals),
            "required_wei": str(exc.required_wei),
            "required": format_units(exc.required_wei, decimals),
            "suggested_command": (
                f"wallet approve set {exc.token_symbol} {exc.spender} "
                f"{format_units(exc.required_wei, decimals)}"
            ),
        },
    )


@app.command("mint")
def mint(
    token_a: str = typer.Argument(..., help="First token (symbol / address / 'ETH' for native)"),
    token_b: str = typer.Argument(..., help="Second token"),
    fee: int = typer.Option(..., "--fee", help="Pool fee tier (100/500/3000/10000)"),
    tick_lower: int = typer.Option(..., "--tick-lower", help="Aligned to fee-tier tickSpacing"),
    tick_upper: int = typer.Option(..., "--tick-upper", help="Aligned to fee-tier tickSpacing"),
    amount_a: str = typer.Option(..., "--amount-a", help="Desired amount of token_a (human units)"),
    amount_b: str = typer.Option(..., "--amount-b", help="Desired amount of token_b (human units)"),
    slippage_bps: int = typer.Option(50, "--slippage-bps", help="Slippage tolerance (default 50 = 0.5%)"),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass"),
    request_id: str | None = typer.Option(None, "--request-id"),
) -> None:
    """Open a new Uniswap V3 LP position.

    Token order is for convenience; we sort to (token0 < token1) before
    encoding. Ticks must be pre-aligned to the fee tier's `tickSpacing`
    (100→1, 500→10, 3000→60, 10000→200) — misalignment raises a
    validation error instead of silently rounding.
    """
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="lp.mint")

    try:
        sender = resolve_account(state, account)
    except typer.BadParameter as e:
        emit_error("validation_error", command="lp.mint", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        ti_a = _resolve_token_or_native(w3, cfg, state, token_a)
        ti_b = _resolve_token_or_native(w3, cfg, state, token_b)
    except ValueError as e:
        emit_error("not_found", command="lp.mint", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        amt_a_wei = parse_units(amount_a, ti_a.decimals)
        amt_b_wei = parse_units(amount_b, ti_b.decimals)
    except ValueError as e:
        emit_error("validation_error", command="lp.mint", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        prepared = prepare_mint(
            w3, cfg, sender.address,
            ti_a, amt_a_wei, ti_b, amt_b_wei,
            fee=fee, tick_lower=tick_lower, tick_upper=tick_upper,
            slippage_bps=slippage_bps,
        )
    except InsufficientAllowance as e:
        # Pick decimals from whichever side the allowance error refers to.
        decimals = (
            ti_a.decimals if e.token_address.lower() == ti_a.address.lower() else ti_b.decimals
        )
        _allowance_error_envelope(
            command="lp.mint", chain_name=cfg.name, exc=e, decimals=decimals,
        )
        raise typer.Exit(code=2)
    except _ContractLogicError as e:
        emit_error(
            "simulation_reverted",
            command="lp.mint", chain=cfg.name,
            reason=_format_revert(e),
        )
        raise typer.Exit(code=3)
    except RuntimeError as e:
        emit_error(
            "simulation_reverted",
            command="lp.mint", chain=cfg.name,
            reason=str(e),
        )
        raise typer.Exit(code=3)
    except ValueError as e:
        emit_error("validation_error", command="lp.mint", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast, yes=yes,
        policy_bypass=policy_bypass, request_id=request_id,
    )


@app.command("increase")
def increase(
    token_id: int = typer.Argument(..., help="NFT tokenId of an existing position"),
    token_a: str = typer.Argument(..., help="First token (symbol / address / 'ETH')"),
    token_b: str = typer.Argument(..., help="Second token"),
    amount_a: str = typer.Option(..., "--amount-a", help="Desired amount of token_a (human units)"),
    amount_b: str = typer.Option(..., "--amount-b", help="Desired amount of token_b (human units)"),
    slippage_bps: int = typer.Option(50, "--slippage-bps"),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass"),
    request_id: str | None = typer.Option(None, "--request-id"),
) -> None:
    """Add liquidity to an existing position. Token pair must match the position's
    on-chain (token0, token1) — order on the CLI is normalized automatically."""
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="lp.increase")

    try:
        sender = resolve_account(state, account)
    except typer.BadParameter as e:
        emit_error("validation_error", command="lp.increase", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        ti_a = _resolve_token_or_native(w3, cfg, state, token_a)
        ti_b = _resolve_token_or_native(w3, cfg, state, token_b)
    except ValueError as e:
        emit_error("not_found", command="lp.increase", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        amt_a_wei = parse_units(amount_a, ti_a.decimals)
        amt_b_wei = parse_units(amount_b, ti_b.decimals)
    except ValueError as e:
        emit_error("validation_error", command="lp.increase", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        prepared = prepare_increase_liquidity(
            w3, cfg, sender.address, token_id,
            ti_a, amt_a_wei, ti_b, amt_b_wei,
            slippage_bps=slippage_bps,
        )
    except InsufficientAllowance as e:
        decimals = (
            ti_a.decimals if e.token_address.lower() == ti_a.address.lower() else ti_b.decimals
        )
        _allowance_error_envelope(
            command="lp.increase", chain_name=cfg.name, exc=e, decimals=decimals,
        )
        raise typer.Exit(code=2)
    except _ContractLogicError as e:
        emit_error(
            "simulation_reverted",
            command="lp.increase", chain=cfg.name,
            reason=_format_revert(e),
        )
        raise typer.Exit(code=3)
    except RuntimeError as e:
        emit_error(
            "simulation_reverted",
            command="lp.increase", chain=cfg.name,
            reason=str(e),
        )
        raise typer.Exit(code=3)
    except ValueError as e:
        emit_error("validation_error", command="lp.increase", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast, yes=yes,
        policy_bypass=policy_bypass, request_id=request_id,
    )

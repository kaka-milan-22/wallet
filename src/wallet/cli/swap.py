from __future__ import annotations

import typer

from wallet.cli._common import confirm_and_broadcast, make_web3_or_exit, resolve_account
from wallet.cli._output import emit_error
from wallet.core.config import get_chain
from wallet.core.rpc import parse_units
from wallet.core.tokens import InsufficientAllowance, TokenInfo, resolve_token
from wallet.protocols.routes.auto import AutoFallbackRoute
from wallet.protocols.routes.base import NoRouteError
from wallet.protocols.routes.uniswap_v3 import UniswapV3DirectRoute
from wallet.protocols.routes.zerox import ZeroExRoute
from wallet.protocols.swap import prepare_swap
from wallet.storage.state import load_state


def _resolve_token_or_native(w3, chain, state, query: str) -> TokenInfo:
    """Like resolve_token but also accepts the chain's native symbol (e.g. 'ETH').

    Native ETH gets a synthetic TokenInfo with symbol=native_symbol, address=WETH9
    (so swap calldata can reference WETH), decimals=18. The address presence is
    intentional — UniV3 wraps via msg.value but calldata still needs the WETH
    address for the pool path.
    """
    if query.upper() == chain.native_symbol.upper():
        weth = chain.builtin_tokens.get("WETH")
        if not weth:
            raise ValueError(
                f"Chain {chain.name} has no WETH configured; cannot swap native {chain.native_symbol}"
            )
        # is_native=True is set HERE and only here — the single trusted boundary
        # for "this is real native ETH, not an ERC-20 that happens to be called ETH".
        return TokenInfo(
            symbol=chain.native_symbol, address=weth, decimals=18, is_native=True,
        )
    return resolve_token(w3, chain, state, query)


def _build_provider(name: str):
    n = name.lower()
    if n in ("uniswap-v3", "uniswap_v3"):
        return UniswapV3DirectRoute()
    if n == "0x":
        return ZeroExRoute()
    if n == "auto":
        # 0x aggregator first (best mainnet price); UniV3 direct as fallback
        # for thin liquidity (e.g. Sepolia) or when WALLET_ZEROX_API_KEY isn't set.
        return AutoFallbackRoute([ZeroExRoute(), UniswapV3DirectRoute()])
    raise ValueError(f"unknown route provider: {name!r} (supported: auto / 0x / uniswap-v3)")


def swap(
    token_in: str = typer.Argument(..., help="Input token symbol or 0x address (or 'ETH' for native)"),
    token_out: str = typer.Argument(..., help="Output token symbol or 0x address"),
    amount: str = typer.Argument(..., help="Input amount in human units"),
    slippage_bps: int = typer.Option(50, "--slippage-bps", help="Slippage tolerance in basis points (50 = 0.5%)"),
    via: str = typer.Option("auto", "--via", help="Route provider: auto (0x → uniswap-v3 fallback) / 0x / uniswap-v3"),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run", help="Default is dry-run; pass --broadcast to submit"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass", help="Skip policy gate (TTY-only)"),
    request_id: str | None = typer.Option(None, "--request-id", help="Idempotency key. Required for non-TTY broadcast."),
    wait: bool = typer.Option(False, "--wait", help="Block until tx receipt; merges block/gas/fee into envelope. Exit 5 on revert."),
    wait_timeout: int = typer.Option(60, "--wait-timeout", envvar="WALLET_WAIT_TIMEOUT", help="Seconds to wait for receipt (default 60)."),
) -> None:
    """Swap an ERC-20 / ETH for another ERC-20 / ETH via a DEX route provider.

    Defaults to a single-hop Uniswap V3 swap on the chain's configured router.
    """
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="swap")

    # Sender account
    try:
        sender = resolve_account(state, account)
    except typer.BadParameter as e:
        emit_error("validation_error", command="swap", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    # Tokens
    try:
        ti = _resolve_token_or_native(w3, cfg, state, token_in)
        to_ = _resolve_token_or_native(w3, cfg, state, token_out)
    except ValueError as e:
        emit_error("not_found", command="swap", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    if ti.address.lower() == to_.address.lower():
        emit_error("validation_error", command="swap", chain=cfg.name,
                   reason=f"token_in and token_out resolve to the same address ({ti.address})")
        raise typer.Exit(code=2)

    # Amount
    try:
        amount_in_wei = parse_units(amount, ti.decimals)
    except ValueError as e:
        emit_error("validation_error", command="swap", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    # Provider
    try:
        provider = _build_provider(via)
    except ValueError as e:
        emit_error("validation_error", command="swap", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    # Build the swap
    try:
        prepared = prepare_swap(
            w3=w3, chain=cfg, sender=sender.address,
            token_in=ti, token_out=to_,
            amount_in_wei=amount_in_wei,
            slippage_bps=slippage_bps,
            provider=provider,
        )
    except NoRouteError as e:
        emit_error("no_route", command="swap", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)
    except InsufficientAllowance as e:
        from wallet.core.rpc import format_units
        emit_error(
            "insufficient_allowance",
            command="swap", chain=cfg.name,
            reason=str(e),
            data={
                "token_symbol": e.token_symbol,
                "token_address": e.token_address,
                "spender": e.spender,
                "current_wei": str(e.current_wei),
                "current": format_units(e.current_wei, ti.decimals),
                "required_wei": str(e.required_wei),
                "required": format_units(e.required_wei, ti.decimals),
                "suggested_command": (
                    f"wallet approve set {e.token_symbol} {e.spender} {format_units(e.required_wei, ti.decimals)}"
                ),
            },
        )
        raise typer.Exit(code=2)

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast,
        yes=yes,
        policy_bypass=policy_bypass,
        request_id=request_id,
        wait=wait,
        wait_timeout=wait_timeout,
    )

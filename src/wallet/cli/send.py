from __future__ import annotations

import typer
from rich.console import Console

from wallet.cli._common import (
    confirm_and_broadcast,
    make_web3_or_exit,
    resolve_account,
    resolve_address,
)
from wallet.cli._output import emit_error
from wallet.core.config import get_chain
from wallet.core.rpc import parse_units
from wallet.core.tokens import resolve_token
from wallet.core.tx import (
    InsufficientFundsError,
    prepare_erc20_transfer,
    prepare_native_transfer,
)
from wallet.storage.state import load_state

console = Console()


def send(
    to: str = typer.Argument(..., help="Recipient: 0x address, @alias, or known name"),
    amount: str = typer.Argument(..., help="Amount in human units (e.g. 0.01)"),
    token: str | None = typer.Option(None, "--token", "-t", help="ERC-20 symbol or 0x address (default: native)"),
    account: str | None = typer.Option(None, "--account", "-a", help="Sending account (default: current default)"),
    chain: str | None = typer.Option(None, "--chain", help="Chain name override"),
    broadcast: bool = typer.Option(False, "--broadcast/--dry-run", help="Default is dry-run; pass --broadcast to submit"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    policy_bypass: bool = typer.Option(False, "--policy-bypass", help="Skip policy gate (TTY-only; ignored in agent mode)"),
    request_id: str | None = typer.Option(None, "--request-id", help="Idempotency key (uuid). Required for non-TTY broadcast."),
    wait: bool = typer.Option(False, "--wait", help="Block until tx receipt; merges block/gas/fee into envelope. Exit 5 on revert."),
    wait_timeout: int = typer.Option(60, "--wait-timeout", envvar="WALLET_WAIT_TIMEOUT", help="Seconds to wait for receipt (default 60)."),
) -> None:
    """Send native ETH or an ERC-20 token to an address.

    By default this is a DRY RUN — the tx is built, simulated, and previewed but
    not signed or broadcast. Add `--broadcast` to actually send.
    """
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="send")

    sender = resolve_account(state, account)

    to_addr = resolve_address(state, to)

    try:
        if token is None:
            amount_wei = parse_units(amount, 18)
            prepared = prepare_native_transfer(w3, cfg, sender.address, to_addr, amount_wei)
        else:
            info = resolve_token(w3, cfg, state, token)
            amount_raw = parse_units(amount, info.decimals)
            prepared = prepare_erc20_transfer(
                w3, cfg, sender.address, info.address, to_addr, amount_raw,
                info.symbol, info.decimals,
            )
    except InsufficientFundsError as e:
        # Estimate_gas / simulate failed because the sender's balance can't
        # cover value + gas. Surface as a typed envelope so JSON callers get a
        # parseable response instead of a raw web3.py traceback.
        emit_error(
            "insufficient_funds",
            command="send",
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
        wait=wait,
        wait_timeout=wait_timeout,
    )

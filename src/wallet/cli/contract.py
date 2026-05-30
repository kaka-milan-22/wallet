"""`wallet contract` — generic contract-call escape hatch.

The whole point of this command is to let users sign one-off calls to
contracts the typed surface doesn't cover (Curve, Pendle, GMX, custom
vaults, NFT ops, `transferOwnership`, etc.) without writing a new
`prepare_*` helper. Policy classifies these as `contract_call`, which is
hard-blocked for agent callers and floor-gated by `contract_allowlist` /
`sentinel_blocklist` / native value caps for TTY callers. See ARCHITECTURE
for the typed-vs-generic split.
"""

from __future__ import annotations

import typer

from wallet.cli._common import (
    confirm_and_broadcast,
    make_web3_or_exit,
    resolve_account,
)
from wallet.cli._output import emit_error
from wallet.core.config import get_chain
from wallet.core.rpc import parse_units
from wallet.protocols.contract_call import (
    ArgCoercionError,
    SignatureParseError,
    prepare_contract_call,
)
from wallet.storage.state import load_state

app = typer.Typer(
    help="Generic contract-call escape hatch (TTY-only; agent callers blocked).",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("call")
def call(
    to: str = typer.Argument(..., help="Contract address (0x…)"),
    fn_sig: str = typer.Argument(
        ...,
        help='Function signature, e.g. "transfer(address,uint256)" or "name()"',
    ),
    args: list[str] = typer.Argument(
        None,
        help="Positional args, one per type in the signature. "
             "Arrays as JSON: '[1,2,3]'.",
    ),
    value: str = typer.Option(
        "0", "--value",
        help="Native ETH to attach as msg.value, human units (e.g. 0.01). Default 0.",
    ),
    account: str | None = typer.Option(None, "--account", "-a"),
    chain: str | None = typer.Option(None, "--chain"),
    broadcast: bool = typer.Option(
        False, "--broadcast/--dry-run",
        help="Default is dry-run; pass --broadcast to submit.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    request_id: str | None = typer.Option(
        None, "--request-id",
        help="Idempotency key. Required for non-TTY broadcast.",
    ),
    wait: bool = typer.Option(False, "--wait", help="Block until tx receipt; merges block/gas/fee into envelope. Exit 5 on revert."),
    wait_timeout: int = typer.Option(60, "--wait-timeout", envvar="WALLET_WAIT_TIMEOUT", help="Seconds to wait for receipt (default 60)."),
) -> None:
    """Sign and broadcast an arbitrary contract call.

    Examples:
      wallet contract call 0xToken "transfer(address,uint256)" 0xRecipient 1000000
      wallet contract call 0xVault "withdraw(uint256)" 100000000000000000 --broadcast
      wallet contract call 0xNFT "setApprovalForAll(address,bool)" 0xOperator false
    """
    state = load_state()
    cfg = get_chain(chain or state.default_chain)
    w3 = make_web3_or_exit(cfg, command="contract.call")

    try:
        sender = resolve_account(state, account)
    except typer.BadParameter as e:
        emit_error("validation_error", command="contract.call", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    if not (to.startswith("0x") and len(to) == 42):
        emit_error(
            "validation_error", command="contract.call", chain=cfg.name,
            reason=f"`to` must be a 0x-prefixed 20-byte address, got {to!r}",
        )
        raise typer.Exit(code=2)

    try:
        value_wei = parse_units(value, 18)
    except ValueError as e:
        emit_error("validation_error", command="contract.call", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    try:
        prepared = prepare_contract_call(
            w3=w3, chain=cfg, sender=sender.address,
            to=to, fn_sig=fn_sig, args=list(args or []), value_wei=value_wei,
        )
    except SignatureParseError as e:
        emit_error("validation_error", command="contract.call", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)
    except ArgCoercionError as e:
        emit_error("validation_error", command="contract.call", chain=cfg.name, reason=str(e))
        raise typer.Exit(code=2)

    confirm_and_broadcast(
        w3, state, cfg, sender, prepared,
        dry_run=not broadcast,
        yes=yes,
        policy_bypass=False,  # this command IS the bypass; don't stack
        request_id=request_id,
        wait=wait,
        wait_timeout=wait_timeout,
        reason=f"wallet contract call on {cfg.name}",
    )

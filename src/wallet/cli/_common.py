"""Helpers shared by sending commands (send, approve, revoke)."""

from __future__ import annotations

import typer
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from web3 import Web3

from wallet.cli._output import (
    OutputMode,
    emit,
    emit_error,
    explain,
    info,
    stdout_console,
)
from wallet.core.config import ChainConfig
from wallet.core.rpc import RpcConnectError, format_units
from wallet.core.rpc import make_web3 as _make_web3_raw
from wallet.core.signer import sign_transaction
from wallet.core.tx import PreparedTx, broadcast
from wallet.storage.state import WalletState


def make_web3_or_exit(chain: ChainConfig, *, command: str):
    """Build a Web3 client and convert `RpcConnectError` into a clean
    `rpc_error` envelope + `typer.Exit(1)`. Use this from every CLI command
    instead of calling `core.rpc.make_web3` directly, so RPC failures never
    surface as raw Python tracebacks."""
    try:
        return _make_web3_raw(chain)
    except RpcConnectError as e:
        emit_error("rpc_error", command=command, chain=chain.name, reason=str(e))
        raise typer.Exit(code=1)


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


def _category(prepared: PreparedTx) -> str:
    """Classify into the high-level command name used in audit + JSON envelope."""
    kind = prepared.description.get("kind", "")
    if kind == "swap":
        return "swap"
    if kind == "aave supply":
        return "aave_supply"
    if kind == "aave withdraw":
        return "aave_withdraw"
    if kind == "aave faucet":
        return "aave_faucet"
    if kind == "aave borrow":
        return "aave_borrow"
    if kind == "aave repay":
        return "aave_repay"
    if "approve" in kind:
        return "approve"
    if "transfer" in kind:
        return "send"
    return "unknown"


def _kind_machine(prepared: PreparedTx) -> str:
    """Stable machine-readable kind: native_transfer / erc20_transfer / erc20_approve / swap / aave_*."""
    desc = prepared.description
    kind_raw = desc.get("kind", "")
    if kind_raw == "swap":
        return "swap"
    if kind_raw == "aave supply":
        return "aave_supply"
    if kind_raw == "aave withdraw":
        return "aave_withdraw"
    if kind_raw == "aave faucet":
        return "aave_faucet"
    if kind_raw == "aave borrow":
        return "aave_borrow"
    if kind_raw == "aave repay":
        return "aave_repay"
    if kind_raw == "native transfer":
        return "native_transfer"
    if "approve" in kind_raw:
        return "erc20_approve"
    if "transfer" in kind_raw:
        return "erc20_transfer"
    return "unknown"


def _build_data(
    prepared: PreparedTx,
    chain: ChainConfig,
    *,
    phase: str,
    state: WalletState | None = None,
    extra: dict | None = None,
) -> dict:
    """Pull a structured payload out of a PreparedTx for emit / audit / JSON."""
    desc = prepared.description
    tx = prepared.tx

    payload: dict = {
        "phase": phase,
        "kind": _kind_machine(prepared),
        "from": desc.get("from"),
        "amount_wei": str(desc.get("amount_wei", 0)),
        "amount": format_units(int(desc.get("amount_wei", 0)), int(desc.get("amount_decimals", 18))),
        "unit": desc.get("amount_unit"),
        "decimals": desc.get("amount_decimals"),
        "nonce": tx.get("nonce"),
        "gas": tx.get("gas"),
        "max_fee_per_gas_wei": str(tx.get("maxFeePerGas")),
        "max_priority_fee_per_gas_wei": str(tx.get("maxPriorityFeePerGas")),
        "estimated_fee_wei": str(prepared.estimated_fee_wei),
        "estimated_fee": format_units(prepared.estimated_fee_wei, 18),
    }
    if "to" in desc:
        payload["to"] = desc["to"]
    if "spender" in desc:
        payload["spender"] = desc["spender"]
    if "token_address" in desc:
        payload["token_address"] = desc["token_address"]
    # Swap-specific fields (only present for swap kind)
    for key in (
        "swap_token_in_address", "swap_token_out_address",
        "swap_token_out_symbol", "swap_token_out_decimals",
        "swap_slippage_bps", "swap_route", "swap_provider",
    ):
        if key in desc:
            payload[key] = desc[key]
    # Aave-specific fields (only present for aave_supply / aave_withdraw)
    for key in (
        "aave_action", "aave_asset_address", "aave_pool",
        "aave_current_hf", "aave_estimated_hf_after",
        "aave_withdraw_max", "aave_repay_max", "aave_faucet",
    ):
        if key in desc:
            payload[key] = desc[key]
    if "swap_amount_out_expected_wei" in desc:
        out_dec = int(desc.get("swap_token_out_decimals", 18))
        payload["swap_amount_out_expected_wei"] = str(desc["swap_amount_out_expected_wei"])
        payload["swap_amount_out_expected"] = format_units(int(desc["swap_amount_out_expected_wei"]), out_dec)
    if "swap_amount_out_min_wei" in desc:
        out_dec = int(desc.get("swap_token_out_decimals", 18))
        payload["swap_amount_out_min_wei"] = str(desc["swap_amount_out_min_wei"])
        payload["swap_amount_out_min"] = format_units(int(desc["swap_amount_out_min_wei"]), out_dec)
    if state is not None:
        if "to" in desc:
            label = _label_for(state, desc["to"])
            if label:
                payload["to_label"] = label
        if "spender" in desc:
            label = _label_for(state, desc["spender"])
            if label:
                payload["spender_label"] = label
    if extra:
        payload.update(extra)
    return payload


def _render_preview(state: WalletState, chain: ChainConfig):
    """Return a rich render closure that draws the existing preview panel."""
    def render(envelope: dict) -> None:
        d = envelope["data"]
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column()

        table.add_row("action", d.get("kind", "?"))
        table.add_row("chain", f"{chain.name} (chainId={chain.chain_id})")

        from_label = d.get("from_label", "") or _label_for(state, d.get("from", ""))
        table.add_row("from", f"{d['from']}{' [' + from_label + ']' if from_label else ''}")

        if "to" in d:
            to_label = d.get("to_label", "")
            table.add_row("to", f"{d['to']}{' [' + to_label + ']' if to_label else ''}")
        if "spender" in d:
            sp_label = d.get("spender_label", "")
            table.add_row("spender", f"{d['spender']}{' [' + sp_label + ']' if sp_label else ''}")
        if "token_address" in d:
            table.add_row("token", d["token_address"])

        table.add_row("amount", f"{d['amount']} {d['unit']}")

        # Swap-specific preview rows
        if d.get("kind") == "swap":
            table.add_row("route", d.get("swap_route", "?"))
            table.add_row(
                "expected out",
                f"{d.get('swap_amount_out_expected', '?')} {d.get('swap_token_out_symbol', '?')}",
            )
            slip = d.get("swap_slippage_bps", 0)
            table.add_row(
                f"min out ({slip / 100}% slip)",
                f"{d.get('swap_amount_out_min', '?')} {d.get('swap_token_out_symbol', '?')}",
            )

        # Aave-specific preview rows
        if d.get("kind") in ("aave_supply", "aave_withdraw", "aave_borrow", "aave_repay"):
            def _hf_display(hf_str):
                try:
                    n = float(hf_str)
                    if n < 1.1:
                        return f"[red]{n:.3f}[/red]"
                    if n < 1.5:
                        return f"[yellow]{n:.3f}[/yellow]"
                    return f"[green]{n:.3f}[/green]"
                except (ValueError, TypeError):
                    return "[green]∞ (no debt)[/green]" if hf_str == "inf" else str(hf_str)

            table.add_row("current HF", _hf_display(d.get("aave_current_hf", "?")))
            # Show estimated HF after for ops that reduce HF (borrow/withdraw)
            if d.get("kind") in ("aave_borrow", "aave_withdraw") and "aave_estimated_hf_after" in d:
                table.add_row("estimated HF after", _hf_display(d["aave_estimated_hf_after"]))
                table.add_row("liquidation HF", "[dim]1.000 (Aave revert threshold)[/dim]")
            if d.get("aave_withdraw_max"):
                table.add_row("withdraw mode", "[bold]max (full aToken balance)[/bold]")
            if d.get("aave_repay_max"):
                table.add_row("repay mode", "[bold]max (full variable debt)[/bold]")

        table.add_row("nonce", str(d.get("nonce")))
        table.add_row("gas limit", str(d.get("gas")))
        table.add_row("max fee / gas", f"{format_units(int(d['max_fee_per_gas_wei']), 9)} gwei")
        table.add_row("priority fee", f"{format_units(int(d['max_priority_fee_per_gas_wei']), 9)} gwei")
        table.add_row("est. fee", f"{d['estimated_fee']} {chain.native_symbol}")

        stdout_console().print(Panel(table, title="transaction preview", border_style="cyan"))
    return render


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

    cmd = _category(prepared)

    # --- preview (always shown in rich; emitted as phase=preview in JSON dry-run) ---
    if dry_run:
        envelope = {
            "ok": True,
            "command": cmd,
            "chain": chain.name,
            "data": _build_data(prepared, chain, phase="preview", state=state),
        }
        emit(envelope, _render_preview(state, chain))
        info("[dim]dry-run — not signing or broadcasting[/dim]")
        return

    # In rich mode we still want to show the preview before policy/confirm.
    # In JSON mode we hold the preview data and only emit on terminal events.
    if not OutputMode.json:
        _render_preview(state, chain)({"data": _build_data(prepared, chain, phase="preview", state=state)})

    caller = caller_kind()
    explain(f"caller={caller} request_id={request_id} policy_bypass={policy_bypass}")

    # --- policy gate ---
    decision = policy_mod.evaluate(prepared, state, caller, bypass=policy_bypass)
    explain(f"policy.evaluate → allowed={decision.allowed} severity={decision.severity} reason={decision.reason}")

    if not decision.allowed:
        _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="rejected", request_id=request_id)
        emit_error(
            "policy_block",
            command=cmd,
            chain=chain.name,
            reason=decision.reason,
            data=_build_data(prepared, chain, phase="rejected", state=state),
        )
        if "no-policy-configured" in decision.reason:
            info("[dim]run `wallet policy init` in your terminal, then edit ~/.wallet/policy.json[/dim]")
        raise typer.Exit(code=3)

    if decision.severity == "warn":
        if caller == "agent":
            _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="rejected", request_id=request_id)
            emit_error(
                "policy_block",
                command=cmd, chain=chain.name,
                reason=f"warn-in-agent-mode:{decision.reason}",
            )
            raise typer.Exit(code=3)
        info(f"[yellow]warn:[/yellow] {decision.reason}")
        if not yes:
            if OutputMode.json:
                # Cannot prompt in JSON mode; require explicit --yes
                _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="rejected", request_id=request_id)
                emit_error(
                    "confirmation_required",
                    command=cmd, chain=chain.name,
                    reason="JSON mode requires --yes to proceed past a warning",
                )
                raise typer.Exit(code=4)
            if not Confirm.ask("proceed despite warning?", default=False):
                _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="user_aborted_after_warn", request_id=request_id)
                emit_error("aborted", command=cmd, chain=chain.name, reason="user declined after warning")
                raise typer.Exit(code=1)

    # --- idempotency check ---
    fp = idempotency.fingerprint(prepared, chain)
    explain(f"idempotency.fingerprint={fp[:12]}…")
    if request_id is not None:
        try:
            cached = idempotency.lookup(request_id, fp)
        except idempotency.IdempotencyMismatch as e:
            block = Decision(allowed=False, reason="idempotency-mismatch", severity="block")
            _audit_event(prepared, chain, block, caller, tx_hash=None, outcome="rejected", request_id=request_id)
            emit_error("idempotency_mismatch", command=cmd, chain=chain.name, reason=str(e), request_id=request_id)
            raise typer.Exit(code=3) from None
        if cached is not None:
            _audit_event(prepared, chain, decision, caller, tx_hash=cached.tx_hash, outcome="replayed_idempotent", request_id=request_id)
            replay_data = _build_data(prepared, chain, phase="idempotent_replay", state=state, extra={
                "tx_hash": cached.tx_hash,
                "explorer_url": chain.explorer_tx_url.replace("{tx}", cached.tx_hash) if cached.tx_hash else None,
                "request_id": request_id,
                "outcome": "replayed_idempotent",
                "original_created_at": cached.created_at,
            })
            envelope = {"ok": True, "command": cmd, "chain": chain.name, "data": replay_data}

            def render_replay(_e):
                info(f"[dim]idempotent replay (request_id={request_id}, original from {cached.created_at})[/dim]")
                if cached.tx_hash:
                    stdout_console().print(cached.tx_hash, soft_wrap=True, highlight=False)
                    stdout_console().print(
                        chain.explorer_tx_url.replace("{tx}", cached.tx_hash),
                        soft_wrap=True, style="dim", highlight=False,
                    )

            emit(envelope, render_replay)
            return
    elif caller == "agent":
        block = Decision(allowed=False, reason="missing-request-id-for-agent", severity="block")
        _audit_event(prepared, chain, block, caller, tx_hash=None, outcome="rejected", request_id=None)
        emit_error(
            "missing_request_id",
            command=cmd, chain=chain.name,
            reason="agent broadcast requires --request-id (generate via uuidgen)",
        )
        raise typer.Exit(code=3)

    # --- final user confirm ---
    if not yes:
        if OutputMode.json:
            _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="rejected", request_id=request_id)
            emit_error(
                "confirmation_required",
                command=cmd, chain=chain.name,
                reason="JSON mode requires --yes when broadcasting",
            )
            raise typer.Exit(code=4)
        if not Confirm.ask("send this transaction?", default=False):
            _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="user_aborted", request_id=request_id)
            emit_error("aborted", command=cmd, chain=chain.name, reason="user declined")
            raise typer.Exit(code=1)

    # --- sign + broadcast ---
    try:
        raw = sign_transaction(sender_account, prepared.tx)
        tx_hash = broadcast(w3, raw)
    except Exception as e:
        _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="rejected", request_id=request_id)
        emit_error(
            "rpc_error",
            command=cmd, chain=chain.name,
            reason=f"{type(e).__name__}: {e}",
        )
        raise typer.Exit(code=1) from None
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
            pass

    _audit_event(prepared, chain, decision, caller, tx_hash=tx_hash, outcome="broadcast", request_id=request_id)

    success_data = _build_data(prepared, chain, phase="broadcast", state=state, extra={
        "tx_hash": tx_hash,
        "explorer_url": chain.explorer_tx_url.replace("{tx}", tx_hash),
        "request_id": request_id,
        "outcome": "broadcast",
    })
    envelope = {"ok": True, "command": cmd, "chain": chain.name, "data": success_data}

    def render_success(_e):
        info("[green]submitted:[/green]")
        stdout_console().print(tx_hash, soft_wrap=True, highlight=False)
        stdout_console().print(
            chain.explorer_tx_url.replace("{tx}", tx_hash),
            soft_wrap=True, style="dim", highlight=False,
        )

    emit(envelope, render_success)

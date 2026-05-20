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


def resolve_account(state: WalletState, account: str | None):
    """Resolve the sending account from `--account <name>` or default.

    Centralizes what every send / approve / swap / aave / portfolio command
    used to inline (`find_account` or `get_default_account` + the same two
    error strings). Raises `typer.BadParameter` so callers can convert it to
    a `validation_error` envelope uniformly."""
    if account:
        a = state.find_account(account)
        if not a:
            raise typer.BadParameter(f"unknown account: {account}")
        return a
    a = state.get_default_account()
    if not a:
        raise typer.BadParameter(
            "no default account — run `wallet account use <name>`"
        )
    return a


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


# Single source of truth for classifying a prepared tx.
#
# `description["kind"]` is the free-form label set by each prepare_* helper
# (e.g. "USDC approve", "native transfer", "aave supply"). _classify maps it to:
#   - category: high-level command name surfaced to audit + policy ("approve", "send", …)
#   - kind:     stable machine-readable ID for the JSON envelope ("erc20_approve", "native_transfer", …)
# Adding a new tx type means adding one row here, not editing two parallel ladders.
_CLASSIFY_TABLE: tuple[tuple[str, str, str, str], ...] = (
    # (match_mode, needle, category, machine_kind)
    # `contract call <fn>` is the generic escape hatch. It MUST match before
    # the "contains transfer" / "contains approve" entries below — a function
    # named `transfer(...)` or `approve(...)` would otherwise be misclassified
    # as a typed send / approve and dodge the contract_call agent-block.
    ("contains", "contract call",    "contract_call", "contract_call"),
    ("exact", "swap",                "swap",          "swap"),
    ("exact", "aave supply",         "aave_supply",   "aave_supply"),
    ("exact", "aave withdraw",       "aave_withdraw", "aave_withdraw"),
    ("exact", "aave faucet",         "aave_faucet",   "aave_faucet"),
    ("exact", "aave borrow",         "aave_borrow",   "aave_borrow"),
    ("exact", "aave repay",          "aave_repay",    "aave_repay"),
    ("exact", "uniswap_v3 lp_mint",     "lp_mint",     "lp_mint"),
    ("exact", "uniswap_v3 lp_increase", "lp_increase", "lp_increase"),
    ("exact", "uniswap_v3 lp_decrease", "lp_decrease", "lp_decrease"),
    ("exact", "uniswap_v3 lp_collect",  "lp_collect",  "lp_collect"),
    # Stuck-tx recovery ops (`wallet tx cancel / replace`). Categories are
    # `tx_cancel` / `tx_replace` so audit log and JSON envelope clearly
    # distinguish a recovery action from the underlying op. Policy has its
    # own router in core/policy.py:_category that maps these to `cancel` /
    # `replace` (or delegates replace to the original op's category).
    ("exact", "tx cancel",           "tx_cancel",     "tx_cancel"),
    ("exact", "tx replace",          "tx_replace",    "tx_replace"),
    ("exact", "native transfer",     "send",          "native_transfer"),
    ("contains", "approve",          "approve",       "erc20_approve"),
    ("contains", "transfer",         "send",          "erc20_transfer"),
)


def _classify(prepared: PreparedTx, *, as_category: bool) -> str:
    kind = prepared.description.get("kind", "")
    for mode, needle, category, machine in _CLASSIFY_TABLE:
        hit = kind == needle if mode == "exact" else needle in kind
        if hit:
            return category if as_category else machine
    return "unknown"


def _category(prepared: PreparedTx) -> str:
    """High-level command name for audit / policy ('approve' / 'send' / 'swap' / 'aave_*')."""
    return _classify(prepared, as_category=True)


def _kind_machine(prepared: PreparedTx) -> str:
    """Stable machine-readable kind for the JSON envelope (erc20_approve / native_transfer / …)."""
    return _classify(prepared, as_category=False)


def _warnings_for(prepared: PreparedTx) -> list[dict]:
    """Per-tx soft warnings surfaced to both rich preview and JSON envelope.

    These run regardless of policy — they fire even when `deny_unlimited_approve`
    is False or `policy_bypass=True`, so the user / agent always sees the risk.
    Policy still has the final say on block-vs-allow; warnings are advisory.
    """
    from wallet.core.tokens import MAX_UINT256

    warnings: list[dict] = []
    desc = prepared.description
    kind = desc.get("kind", "")
    amount = int(desc.get("amount_wei", 0))

    if "approve" in kind and amount == MAX_UINT256:
        spender = desc.get("spender", "?")
        token = desc.get("amount_unit", "token")
        warnings.append({
            "code": "unlimited_approve",
            "severity": "high",
            "message": (
                f"Unlimited approval — spender {spender} can drain your entire "
                f"{token} balance, now and in the future. Prefer an exact amount."
            ),
        })
    return warnings


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
        "nonce": tx.get("nonce", "fresh-at-sign-time"),
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
    # Contract-call (generic escape hatch) fields. Keep calldata in the
    # envelope so agents and humans can both diff exactly what's being signed.
    for key in (
        "cc_function_signature", "cc_function_name",
        "cc_args", "cc_calldata", "cc_value_wei",
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
    # Uniswap V3 LP fields (only present for lp_mint / lp_increase / lp_decrease / lp_collect).
    # Stringify uint256-sized values so the JSON envelope is JS-safe.
    for key in (
        "lp_action", "lp_nft_token_id", "lp_nfpm",
        "lp_token0_address", "lp_token1_address",
        "lp_token0_symbol", "lp_token1_symbol",
        "lp_token0_decimals", "lp_token1_decimals",
        "lp_fee", "lp_tick_lower", "lp_tick_upper",
        "lp_slippage_bps", "lp_percent", "lp_recipient",
    ):
        if key in desc:
            payload[key] = desc[key]
    for key in (
        "lp_liquidity_wei",
        "lp_amount0_expected_wei", "lp_amount1_expected_wei",
        "lp_amount0_desired_wei", "lp_amount1_desired_wei",
        "lp_amount0_min_wei", "lp_amount1_min_wei",
        "lp_native_value_wei",
    ):
        if key in desc:
            payload[key] = str(desc[key])
    # Stuck-tx recovery fields (only present for `tx cancel` / `tx replace`).
    # Surfacing these keeps the recovery action distinguishable in audit and
    # JSON output — without them a cancel looks like a 0-value self-send and
    # a replace looks like a regular send.
    for key in (
        "cancel_nonce", "replace_nonce",
        "old_tx_hash", "original_tx_hash", "original_kind",
        "is_self_send_for_cancel", "is_replacement",
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
    warnings = _warnings_for(prepared)
    if warnings:
        payload["warnings"] = warnings
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

        # Generic contract-call preview rows. This is the escape-hatch
        # category — show the signature, decoded args, and raw calldata so
        # the human signer has everything needed to diff against intent.
        if d.get("kind") == "contract_call":
            table.add_row("function", d.get("cc_function_signature", "?"))
            for i, arg in enumerate(d.get("cc_args", []) or []):
                val = arg.get("value")
                if isinstance(val, list):
                    val = "[" + ", ".join(str(x) for x in val) + "]"
                table.add_row(f"  arg[{i}] ({arg.get('type', '?')})", str(val))
            cd = d.get("cc_calldata", "")
            if cd:
                shown = cd if len(cd) <= 138 else cd[:66] + " … " + cd[-66:]
                table.add_row("calldata", shown)

        # Swap-specific preview rows
        if d.get("kind") == "swap":
            # Show the input token's resolved on-chain address so a reader can
            # diff against what they expected to sell. Symbol alone is unsafe —
            # any ERC-20 can claim symbol="ETH". See security_review.md Vuln 1.
            if "swap_token_in_address" in d:
                table.add_row(
                    "token in",
                    f"{d['swap_token_in_address']} ({d.get('unit', '?')})",
                )
            if "swap_token_out_address" in d:
                table.add_row(
                    "token out",
                    f"{d['swap_token_out_address']} ({d.get('swap_token_out_symbol', '?')})",
                )
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

        # Stuck-tx recovery preview rows — make it obvious this isn't a
        # fresh send. The cancel/replace row points at the original tx hash
        # so the human signer can cross-check on Etherscan before approving.
        if d.get("kind") == "tx_cancel":
            old_hash = d.get("old_tx_hash")
            if old_hash:
                table.add_row("replacing", f"[dim]{old_hash}[/dim] (cancel)")
            else:
                table.add_row("recovery", "[bold]cancel — 0-value self-send at locked nonce[/bold]")
        elif d.get("kind") == "tx_replace":
            old_hash = d.get("original_tx_hash")
            orig_kind = d.get("original_kind", "?")
            if old_hash:
                table.add_row("replacing", f"[dim]{old_hash}[/dim] (speedup of {orig_kind})")
            else:
                table.add_row("recovery", f"[bold]replace — re-broadcast original {orig_kind} with bumped gas[/bold]")

        nonce_disp = d.get("nonce")
        table.add_row(
            "nonce",
            "[dim]fresh at sign-time[/dim]" if nonce_disp == "fresh-at-sign-time"
            else str(nonce_disp),
        )
        table.add_row("gas limit", str(d.get("gas")))
        table.add_row("max fee / gas", f"{format_units(int(d['max_fee_per_gas_wei']), 9)} gwei")
        table.add_row("priority fee", f"{format_units(int(d['max_priority_fee_per_gas_wei']), 9)} gwei")
        table.add_row("est. fee", f"{d['estimated_fee']} {chain.native_symbol}")

        stdout_console().print(Panel(table, title="transaction preview", border_style="cyan"))

        for w in d.get("warnings", []):
            sev = w.get("severity", "warn").upper()
            stdout_console().print(
                Panel(
                    f"[bold red]{sev}:[/bold red] {w.get('message', '')}",
                    title=f"⚠ {w.get('code', 'warning')}",
                    border_style="red",
                )
            )
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
    entry: dict = {
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
    }
    # Stuck-tx recovery audit enrichment. Without these fields a cancel
    # entry is indistinguishable from a regular 0-value self-send in the
    # log; a replace looks identical to a fresh send. Forensics needs the
    # original tx_hash and the original op kind.
    kind = desc.get("kind", "")
    if kind == "tx cancel":
        entry["recovery"] = "cancel"
        if "old_tx_hash" in desc:
            entry["old_tx_hash"] = desc["old_tx_hash"]
    elif kind == "tx replace":
        entry["recovery"] = "replace"
        if "original_tx_hash" in desc:
            entry["old_tx_hash"] = desc["original_tx_hash"]
        if "original_kind" in desc:
            entry["original_kind"] = desc["original_kind"]
    try:
        audit.write(entry)
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
    preserve_nonce: bool = False,
) -> None:
    """Drive a PreparedTx through preview / policy / idempotency / sign / broadcast.

    `preserve_nonce=True` skips the sign-time nonce refresh. Used by stuck-tx
    recovery (cancel / replace) where the PreparedTx is pinned to a specific
    pre-existing mempool nonce — refreshing would defeat the EIP-1559
    replacement semantics.
    """
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
            # `replayed: true` is a top-level flag so agents can distinguish
            # cache-hit from a fresh broadcast without parsing data.phase.
            # See security_review.md Vuln 2.
            envelope = {
                "ok": True,
                "replayed": True,
                "command": cmd,
                "chain": chain.name,
                "data": replay_data,
            }

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
    # Refresh nonce *here*, not at prepare time. Any time between dry-run /
    # preview / policy prompt and the actual broadcast another tx from the same
    # account could have landed (e.g. an idempotent retry that did make it),
    # which would make a baked-in nonce stale. Reading from "pending" includes
    # already-submitted-but-unmined txs in our own mempool slot, so successive
    # broadcasts in the same script work too.
    if preserve_nonce:
        if "nonce" not in prepared.tx:
            _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="rejected", request_id=request_id)
            emit_error(
                "validation_error",
                command=cmd, chain=chain.name,
                reason="preserve_nonce=True but PreparedTx has no nonce field",
            )
            raise typer.Exit(code=1)
        explain(f"nonce preserved (stuck-tx recovery) = {prepared.tx['nonce']}")
    else:
        try:
            prepared.tx["nonce"] = w3.eth.get_transaction_count(
                Web3.to_checksum_address(prepared.tx["from"]), "pending"
            )
        except Exception as e:
            _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="rejected", request_id=request_id)
            emit_error(
                "rpc_error",
                command=cmd, chain=chain.name,
                reason=f"failed to refresh nonce: {type(e).__name__}: {e}",
            )
            raise typer.Exit(code=1) from None
        explain(f"nonce refreshed at sign-time → {prepared.tx['nonce']}")

    try:
        raw = sign_transaction(sender_account, prepared.tx)
        tx_hash = broadcast(w3, raw)
    except Exception as e:
        # Stuck-tx recovery (preserve_nonce=True) has a known benign failure:
        # the original tx mined while we were preparing the cancel/replace,
        # so RPC rejects the new tx with "nonce too low". Surface that as
        # `outcome=superseded` instead of a generic rpc_error — both audit
        # and the JSON envelope encode the race outcome cleanly.
        err_msg = str(e).lower()
        is_superseded = preserve_nonce and (
            "nonce too low" in err_msg
            or "already known" in err_msg
            or "replacement transaction underpriced" in err_msg
        )
        if is_superseded:
            if request_id is not None:
                try:
                    idempotency.record(
                        request_id, fp,
                        tx_hash=None,
                        nonce=prepared.tx.get("nonce"),
                        outcome="superseded",
                        detail="original_landed_first",
                        from_address=prepared.description.get("from") or prepared.tx.get("from"),
                        description=dict(prepared.description),
                    )
                except Exception:
                    pass
            _audit_event(prepared, chain, decision, caller, tx_hash=None, outcome="superseded", request_id=request_id)
            data = _build_data(prepared, chain, phase="superseded", state=state, extra={
                "outcome": "superseded",
                "reason": "original_landed_first",
                "request_id": request_id,
                "rpc_error": f"{type(e).__name__}: {e}",
            })
            envelope = {"ok": False, "command": cmd, "chain": chain.name, "code": "superseded", "data": data}
            emit(envelope, lambda _d: info(
                f"[yellow]superseded[/yellow] — the original tx at nonce "
                f"{prepared.tx.get('nonce')} landed before this recovery "
                f"could replace it (nothing to do)."
            ))
            raise typer.Exit(code=0) from None
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
                from_address=prepared.description.get("from") or prepared.tx.get("from"),
                description=dict(prepared.description),
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

from __future__ import annotations

import typer
from rich.panel import Panel
from rich.table import Table

from wallet.cli._caller import caller_kind, is_agent
from wallet.cli._output import OutputMode, emit, emit_error, info, stdout_console
from wallet.core.policy import (
    Policy,
    default_policy,
    load_policy,
    policy_path,
    save_policy,
)

app = typer.Typer(no_args_is_help=True, help="Configure agent-callable policy gates")


@app.command("show")
def show() -> None:
    """Print the current policy and detected caller mode."""
    p = load_policy()
    data = {
        "ok": True,
        "command": "policy.show",
        "data": {
            "caller": caller_kind(),
            "path": str(policy_path()),
            "configured": p is not None,
            "policy": p.model_dump() if p else None,
        },
    }

    def render(d):
        x = d["data"]
        stdout_console().print(f"caller: [bold]{x['caller']}[/bold]")
        stdout_console().print(f"path:   [dim]{x['path']}[/dim]")
        if not x["configured"]:
            info("[red]no policy file — agent broadcasts denied by default[/red]")
            info("[dim]run `wallet policy init` in your terminal to create one[/dim]")
            return
        # Use the loaded policy object for pretty printing
        stdout_console().print()
        stdout_console().print(Panel(
            Policy(**x["policy"]).model_dump_json(indent=2),
            title="policy", border_style="cyan",
        ))

    emit(data, render)


@app.command("init")
def init(force: bool = typer.Option(False, "--force", help="Overwrite an existing policy.json")) -> None:
    """Create ~/.wallet/policy.json with safe defaults. TTY-only."""
    if is_agent() or OutputMode.json:
        emit_error(
            "tty_required",
            command="policy.init",
            reason="policy init must run in an interactive terminal (writing the policy from "
                   "an agent context would let an agent raise its own caps)",
        )
        raise typer.Exit(code=2)

    p = policy_path()
    if p.exists() and not force:
        emit_error(
            "validation_error",
            command="policy.init",
            reason=f"policy already exists at {p} — pass --force to overwrite",
        )
        raise typer.Exit(code=1)

    save_policy(default_policy())
    emit(
        {"ok": True, "command": "policy.init", "data": {"path": str(p)}},
        lambda d: stdout_console().print(
            f"[green]wrote default policy to {d['data']['path']}[/green]\n"
            "[dim]edit the file in your terminal to add allowlist entries and tune caps.[/dim]"
        ),
    )


@app.command("lint")
def lint() -> None:
    """Validate schema and warn about weak / missing settings."""
    p = load_policy()
    if p is None:
        emit_error("not_found", command="policy.lint",
                   reason="no policy file — run `wallet policy init`")
        raise typer.Exit(code=1)

    warnings: list[str] = []
    if not p.max_per_tx:
        warnings.append("max_per_tx is empty — no per-tx amount cap")
    if not p.max_per_day:
        warnings.append("max_per_day is empty — no daily cap")
    if not p.recipient_allowlist:
        warnings.append("recipient_allowlist is empty — sends will be blocked until you add entries")
    if not p.contract_allowlist:
        warnings.append("contract_allowlist is empty — approves will be blocked")
    if not p.deny_unlimited_approve:
        warnings.append("deny_unlimited_approve=False — agent could approve max uint256 to a contract")
    if not p.first_send_warn:
        warnings.append("first_send_warn=False — agent can send to brand-new addresses without warning")

    data = {
        "ok": True,
        "command": "policy.lint",
        "data": {"warnings": warnings, "ok": len(warnings) == 0},
    }

    def render(d):
        table = Table(show_header=True, header_style="bold")
        table.add_column("level")
        table.add_column("message")
        if d["data"]["ok"]:
            table.add_row("[green]OK[/green]", "policy looks reasonable")
        else:
            for w in d["data"]["warnings"]:
                table.add_row("[yellow]WARN[/yellow]", w)
        stdout_console().print(table)

    emit(data, render)

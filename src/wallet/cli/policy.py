from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from wallet.cli._caller import caller_kind, is_agent
from wallet.core.policy import (
    Policy,
    default_policy,
    load_policy,
    policy_path,
    save_policy,
)

app = typer.Typer(no_args_is_help=True, help="Configure agent-callable policy gates")
console = Console()


@app.command("show")
def show() -> None:
    """Print the current policy and detected caller mode."""
    p = load_policy()
    console.print(f"caller: [bold]{caller_kind()}[/bold]")
    console.print(f"path:   [dim]{policy_path()}[/dim]")
    if p is None:
        console.print("[red]no policy file — agent broadcasts denied by default[/red]")
        console.print("[dim]run `wallet policy init` in your terminal to create one[/dim]")
        return
    console.print()
    console.print(Panel(p.model_dump_json(indent=2), title="policy", border_style="cyan"))


@app.command("init")
def init(force: bool = typer.Option(False, "--force", help="Overwrite an existing policy.json")) -> None:
    """Create ~/.wallet/policy.json with safe defaults. TTY-only.

    Editing the policy must be done by a human in a terminal — letting an
    agent rewrite the policy would defeat the purpose of having one.
    """
    if is_agent():
        console.print(
            "[red]wallet policy init is TTY-only.[/red]\n"
            "[dim]editing the policy from a non-interactive context would let an agent\n"
            "raise its own caps. Run this command directly in your terminal.[/dim]"
        )
        raise typer.Exit(code=2)

    p = policy_path()
    if p.exists() and not force:
        console.print(f"[yellow]policy already exists at {p} — pass --force to overwrite[/yellow]")
        raise typer.Exit(code=1)

    save_policy(default_policy())
    console.print(f"[green]wrote default policy to {p}[/green]")
    console.print(
        "[dim]edit the file in your terminal to add allowlist entries and tune caps.[/dim]"
    )


@app.command("lint")
def lint() -> None:
    """Validate schema and warn about weak / missing settings."""
    p = load_policy()
    if p is None:
        console.print("[red]no policy file — run `wallet policy init`[/red]")
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

    table = Table(show_header=True, header_style="bold")
    table.add_column("level")
    table.add_column("message")
    for w in warnings:
        table.add_row("[yellow]WARN[/yellow]", w)
    if not warnings:
        table.add_row("[green]OK[/green]", "policy looks reasonable")
    console.print(table)

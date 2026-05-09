from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from wallet.core import hd
from wallet.storage import vault
from wallet.storage.state import AccountEntry, load_state, save_state

app = typer.Typer(no_args_is_help=True, help="Manage HD accounts")
console = Console()


def _vault_key(name: str) -> str:
    # agent-vault keys must be lowercase alphanumeric + hyphens.
    safe = name.lower().replace("_", "-")
    return f"wallet-{safe}-mnemonic"


def _print_set_instructions(vault_key: str) -> None:
    console.print(
        Panel(
            f"[bold]Next step (in your terminal, NOT inside an agent):[/bold]\n\n"
            f"  [cyan]agent-vault set {vault_key}[/cyan]\n\n"
            f"When prompted, paste the mnemonic above. The wallet CLI will then\n"
            f"be able to sign transactions for this account.",
            title="agent-vault setup",
            border_style="yellow",
        )
    )


@app.command("create")
def create(
    name: str = typer.Argument(..., help="Account name (also used in vault key)"),
    set_default: bool = typer.Option(True, "--default/--no-default", help="Set as default account"),
) -> None:
    """Generate a fresh BIP-39 mnemonic and register a new account at index 0.

    The mnemonic is shown ONCE. Write it down somewhere safe AND store it in
    agent-vault using the printed instruction.
    """
    state = load_state()
    if state.find_account(name):
        console.print(f"[red]account '{name}' already exists[/red]")
        raise typer.Exit(code=1)

    vault_key = _vault_key(name)
    if vault.has(vault_key):
        console.print(
            f"[red]vault already has key '{vault_key}' — refusing to overwrite[/red]"
        )
        raise typer.Exit(code=1)

    mnemonic = hd.generate_mnemonic()
    derived = hd.derive(mnemonic, index=0)

    console.print(
        Panel(
            f"[bold red]MNEMONIC (shown only once — write it down):[/bold red]\n\n"
            f"[bold yellow]{mnemonic}[/bold yellow]",
            border_style="red",
        )
    )
    console.print(f"address: [cyan]{derived.address}[/cyan]")
    console.print(f"path:    [dim]{derived.path}[/dim]")
    _print_set_instructions(vault_key)

    state.accounts.append(
        AccountEntry(
            name=name,
            address=derived.address,
            derivation_path=derived.path,
            vault_key=vault_key,
        )
    )
    if set_default or state.default_account is None:
        state.default_account = name
    save_state(state)


@app.command("import")
def import_(
    name: str = typer.Argument(..., help="Account name"),
    index: int = typer.Option(0, "--index", "-i", help="BIP-44 derivation index"),
    set_default: bool = typer.Option(False, "--default/--no-default", help="Set as default account"),
) -> None:
    """Import an existing mnemonic and register an account.

    You will be prompted to paste the mnemonic (hidden input). It is held in
    memory only long enough to derive the address; you must then run
    `agent-vault set wallet/<name>/mnemonic` to store it.
    """
    state = load_state()
    if state.find_account(name):
        console.print(f"[red]account '{name}' already exists[/red]")
        raise typer.Exit(code=1)

    vault_key = _vault_key(name)

    mnemonic = Prompt.ask("mnemonic (hidden)", password=True).strip()
    if not hd.is_valid_mnemonic(mnemonic):
        console.print("[red]invalid BIP-39 mnemonic[/red]")
        raise typer.Exit(code=1)

    derived = hd.derive(mnemonic, index=index)
    del mnemonic  # drop reference; CPython GC will reclaim

    console.print(f"address: [cyan]{derived.address}[/cyan]")
    console.print(f"path:    [dim]{derived.path}[/dim]")

    if not vault.has(vault_key):
        _print_set_instructions(vault_key)
    else:
        console.print(f"[green]vault already has '{vault_key}' — reusing[/green]")

    state.accounts.append(
        AccountEntry(
            name=name,
            address=derived.address,
            derivation_path=derived.path,
            vault_key=vault_key,
        )
    )
    if set_default or state.default_account is None:
        state.default_account = name
    save_state(state)


@app.command("list")
def list_() -> None:
    """List all registered accounts."""
    state = load_state()
    if not state.accounts:
        console.print("[dim]no accounts yet — run `wallet account create <name>`[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("address")
    table.add_column("path", style="dim")
    table.add_column("vault key", style="dim")
    table.add_column("default")
    for a in state.accounts:
        table.add_row(
            a.name,
            a.address,
            a.derivation_path,
            a.vault_key,
            "★" if a.name == state.default_account else "",
        )
    console.print(table)


@app.command("show")
def show(name: str = typer.Argument(...)) -> None:
    """Show account details + whether the vault key is populated."""
    state = load_state()
    a = state.find_account(name)
    if not a:
        console.print(f"[red]no account named '{name}'[/red]")
        raise typer.Exit(code=1)

    console.print(f"name:    [bold]{a.name}[/bold]")
    console.print(f"address: [cyan]{a.address}[/cyan]")
    console.print(f"path:    [dim]{a.derivation_path}[/dim]")
    console.print(f"vault:   [dim]{a.vault_key}[/dim]")
    console.print(
        f"signed:  [{'green' if vault.has(a.vault_key) else 'red'}]"
        f"{'yes (vault populated)' if vault.has(a.vault_key) else 'no — run agent-vault set ' + a.vault_key}[/]"
    )


@app.command("derive")
def derive(
    source: str = typer.Argument(..., help="Source account whose vault key to reuse"),
    index: int = typer.Option(..., "--index", "-i", help="BIP-44 derivation index"),
    name: str = typer.Option(..., "--as", "-n", help="Name for the new derived account"),
) -> None:
    """Derive a new sub-account from an existing account's mnemonic.

    The two accounts share the same vault key (one mnemonic, many addresses).
    """
    state = load_state()
    src = state.find_account(source)
    if not src:
        console.print(f"[red]no source account '{source}'[/red]")
        raise typer.Exit(code=1)
    if state.find_account(name):
        console.print(f"[red]account '{name}' already exists[/red]")
        raise typer.Exit(code=1)
    if not vault.has(src.vault_key):
        console.print(
            f"[red]vault key '{src.vault_key}' is empty — store the mnemonic first[/red]"
        )
        raise typer.Exit(code=1)

    mnemonic = vault.reveal(src.vault_key)
    try:
        derived = hd.derive(mnemonic, index=index)
    finally:
        del mnemonic

    state.accounts.append(
        AccountEntry(
            name=name,
            address=derived.address,
            derivation_path=derived.path,
            vault_key=src.vault_key,
        )
    )
    save_state(state)
    console.print(f"derived [cyan]{derived.address}[/cyan] at [dim]{derived.path}[/dim]")


@app.command("use")
def use(name: str = typer.Argument(...)) -> None:
    """Set the default account."""
    state = load_state()
    if not state.find_account(name):
        console.print(f"[red]no account named '{name}'[/red]")
        raise typer.Exit(code=1)
    state.default_account = name
    save_state(state)
    console.print(f"default → [bold]{name}[/bold]")

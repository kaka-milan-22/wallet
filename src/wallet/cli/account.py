from __future__ import annotations

import typer
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from wallet.cli._output import OutputMode, emit, emit_error, info, stdout_console
from wallet.core import hd
from wallet.storage import vault
from wallet.storage.state import AccountEntry, load_state, save_state

app = typer.Typer(no_args_is_help=True, help="Manage HD accounts")


def _vault_key(name: str) -> str:
    safe = name.lower().replace("_", "-")
    return f"wallet-{safe}-mnemonic"


def _refuse_in_json(command: str) -> None:
    """Mnemonic-bearing flows must never run while wallet's stdout is feeding
    JSON into an agent's context."""
    if OutputMode.json:
        emit_error(
            "tty_required",
            command=command,
            reason="account create / import shows or accepts the mnemonic on stdin/stdout. "
                   "JSON mode would route that into an LLM context. Run in your terminal directly.",
        )
        raise typer.Exit(code=2)


def _print_set_instructions(vault_key: str) -> None:
    stdout_console().print(
        Panel(
            f"[bold]Next step (in your terminal, NOT inside an agent):[/bold]\n\n"
            f"  [cyan]alice set {vault_key}[/cyan]\n\n"
            f"When prompted, paste the mnemonic above. The wallet CLI will then\n"
            f"be able to sign transactions for this account (requires bob running).",
            title="alice (AnB) setup",
            border_style="yellow",
        )
    )


@app.command("create")
def create(
    name: str = typer.Argument(..., help="Account name (also used in vault key)"),
    set_default: bool = typer.Option(True, "--default/--no-default", help="Set as default account"),
) -> None:
    """Generate a fresh BIP-39 mnemonic and register a new account at index 0."""
    _refuse_in_json("account.create")

    state = load_state()
    if state.find_account(name):
        emit_error("validation_error", command="account.create",
                   reason=f"account '{name}' already exists")
        raise typer.Exit(code=1)

    vault_key = _vault_key(name)
    if vault.has(vault_key):
        emit_error("validation_error", command="account.create",
                   reason=f"vault already has key '{vault_key}' — refusing to overwrite")
        raise typer.Exit(code=1)

    mnemonic = hd.generate_mnemonic()
    derived = hd.derive(mnemonic, index=0)

    stdout_console().print(
        Panel(
            f"[bold red]MNEMONIC (shown only once — write it down):[/bold red]\n\n"
            f"[bold yellow]{mnemonic}[/bold yellow]",
            border_style="red",
        )
    )
    stdout_console().print(f"address: [cyan]{derived.address}[/cyan]")
    stdout_console().print(f"path:    [dim]{derived.path}[/dim]")
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
    """Import an existing mnemonic and register an account."""
    _refuse_in_json("account.import")

    state = load_state()
    if state.find_account(name):
        emit_error("validation_error", command="account.import",
                   reason=f"account '{name}' already exists")
        raise typer.Exit(code=1)

    vault_key = _vault_key(name)

    mnemonic = Prompt.ask("mnemonic (hidden)", password=True).strip()
    if not hd.is_valid_mnemonic(mnemonic):
        emit_error("validation_error", command="account.import",
                   reason="invalid BIP-39 mnemonic")
        raise typer.Exit(code=1)

    derived = hd.derive(mnemonic, index=index)
    del mnemonic

    stdout_console().print(f"address: [cyan]{derived.address}[/cyan]")
    stdout_console().print(f"path:    [dim]{derived.path}[/dim]")

    if not vault.has(vault_key):
        _print_set_instructions(vault_key)
    else:
        info(f"[green]vault already has '{vault_key}' — reusing[/green]")

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
    rows = [
        {
            "name": a.name,
            "address": a.address,
            "derivation_path": a.derivation_path,
            "vault_key": a.vault_key,
            "default": a.name == state.default_account,
        }
        for a in state.accounts
    ]
    data = {
        "ok": True, "command": "account.list",
        "data": {"default_account": state.default_account, "accounts": rows},
    }

    def render(d):
        if not d["data"]["accounts"]:
            info("[dim]no accounts yet — run `wallet account create <name>`[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("name")
        table.add_column("address")
        table.add_column("path", style="dim")
        table.add_column("vault key", style="dim")
        table.add_column("default")
        for a in d["data"]["accounts"]:
            table.add_row(
                a["name"], a["address"], a["derivation_path"], a["vault_key"],
                "★" if a["default"] else "",
            )
        stdout_console().print(table)

    emit(data, render)


@app.command("show")
def show(name: str = typer.Argument(...)) -> None:
    """Show account details + whether the vault key is populated."""
    state = load_state()
    a = state.find_account(name)
    if not a:
        emit_error("not_found", command="account.show", reason=f"no account named '{name}'")
        raise typer.Exit(code=1)

    signed = vault.has(a.vault_key)
    data = {
        "ok": True, "command": "account.show",
        "data": {
            "name": a.name,
            "address": a.address,
            "derivation_path": a.derivation_path,
            "vault_key": a.vault_key,
            "signed": signed,
        },
    }

    def render(d):
        x = d["data"]
        c = stdout_console()
        c.print(f"name:    [bold]{x['name']}[/bold]")
        c.print(f"address: [cyan]{x['address']}[/cyan]")
        c.print(f"path:    [dim]{x['derivation_path']}[/dim]")
        c.print(f"vault:   [dim]{x['vault_key']}[/dim]")
        if x["signed"]:
            c.print("signed:  [green]yes (vault populated)[/green]")
        else:
            c.print(f"signed:  [red]no — run alice set {x['vault_key']}[/red]")

    emit(data, render)


@app.command("derive")
def derive(
    source: str = typer.Argument(..., help="Source account whose vault key to reuse"),
    index: int = typer.Option(..., "--index", "-i", help="BIP-44 derivation index"),
    name: str = typer.Option(..., "--as", "-n", help="Name for the new derived account"),
) -> None:
    """Derive a new sub-account from an existing account's mnemonic."""
    state = load_state()
    src = state.find_account(source)
    if not src:
        emit_error("not_found", command="account.derive",
                   reason=f"no source account '{source}'")
        raise typer.Exit(code=1)
    if state.find_account(name):
        emit_error("validation_error", command="account.derive",
                   reason=f"account '{name}' already exists")
        raise typer.Exit(code=1)
    if not vault.has(src.vault_key):
        emit_error("vault_error", command="account.derive",
                   reason=f"vault key '{src.vault_key}' is empty — store the mnemonic first")
        raise typer.Exit(code=1)

    try:
        mnemonic = vault.reveal(src.vault_key)
    except Exception as e:
        # vault errors never carry mnemonic data — safe to surface `str(e)`.
        emit_error("vault_error", command="account.derive",
                   reason=f"{type(e).__name__}: {e}")
        raise typer.Exit(code=1)

    try:
        derived_acct = hd.derive(mnemonic, index=index)
    except hd.MnemonicError as e:
        # `MnemonicError.__str__` is pre-sanitized by `hd.derive`; this
        # branch exists to make it explicit that even if someone widens
        # the except to `Exception`, the leaky `ValidationError` from
        # eth_account is already converted at the boundary.
        emit_error("vault_error", command="account.derive", reason=str(e))
        raise typer.Exit(code=1)
    finally:
        try:
            del mnemonic
        except NameError:
            pass

    state.accounts.append(
        AccountEntry(
            name=name,
            address=derived_acct.address,
            derivation_path=derived_acct.path,
            vault_key=src.vault_key,
        )
    )
    save_state(state)
    emit(
        {
            "ok": True, "command": "account.derive",
            "data": {
                "name": name,
                "address": derived_acct.address,
                "derivation_path": derived_acct.path,
                "vault_key": src.vault_key,
                "source": source,
            },
        },
        lambda d: stdout_console().print(
            f"derived [cyan]{d['data']['address']}[/cyan] at [dim]{d['data']['derivation_path']}[/dim]"
        ),
    )


@app.command("use")
def use(name: str = typer.Argument(...)) -> None:
    """Set the default account."""
    state = load_state()
    if not state.find_account(name):
        emit_error("not_found", command="account.use", reason=f"no account named '{name}'")
        raise typer.Exit(code=1)
    state.default_account = name
    save_state(state)
    emit(
        {"ok": True, "command": "account.use", "data": {"default_account": name}},
        lambda d: stdout_console().print(f"default → [bold]{d['data']['default_account']}[/bold]"),
    )

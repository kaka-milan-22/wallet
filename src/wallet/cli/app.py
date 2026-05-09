from __future__ import annotations

import typer
from rich.console import Console

from wallet import __version__
from wallet.cli import account as account_cli
from wallet.cli import approve as approve_cli
from wallet.cli import book as book_cli
from wallet.cli import token as token_cli
from wallet.cli import watch as watch_cli
from wallet.cli.balance import balance as balance_cmd
from wallet.cli.history import history as history_cmd
from wallet.cli.send import send as send_cmd

app = typer.Typer(
    help="DeFi CLI wallet — Phase 1 (account / transfer / approve / history).",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(account_cli.app, name="account")
app.add_typer(approve_cli.app, name="approve")
app.add_typer(book_cli.app, name="book")
app.add_typer(watch_cli.app, name="watch")
app.add_typer(token_cli.app, name="token")
app.command("balance")(balance_cmd)
app.command("send")(send_cmd)
app.command("history")(history_cmd)

console = Console()


@app.command()
def version() -> None:
    """Print wallet CLI version."""
    console.print(f"wallet [bold cyan]{__version__}[/bold cyan]")


@app.command()
def info() -> None:
    """Show config paths and default chain."""
    from wallet.core.config import get_chain
    from wallet.storage.state import state_path

    state = state_path()
    chain = get_chain("sepolia")
    console.print(f"state file:   [dim]{state}[/dim]")
    console.print(f"default chain:[dim] {chain.name} (chainId={chain.chain_id})[/dim]")
    console.print(f"rpc:          [dim]{chain.rpc_url}[/dim]")
    console.print(f"explorer:     [dim]{chain.explorer_tx_url}[/dim]")


if __name__ == "__main__":
    app()

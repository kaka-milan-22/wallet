from __future__ import annotations

import typer

from wallet import __version__
from wallet.cli import account as account_cli
from wallet.cli import approve as approve_cli
from wallet.cli import book as book_cli
from wallet.cli import policy as policy_cli
from wallet.cli import token as token_cli
from wallet.cli import watch as watch_cli
from wallet.cli._output import OutputMode, emit, stdout_console
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
app.add_typer(policy_cli.app, name="policy")
app.command("balance")(balance_cmd)
app.command("send")(send_cmd)
app.command("history")(history_cmd)


@app.callback()
def _global(
    json_output: bool = typer.Option(
        False, "--json", envvar="WALLET_JSON",
        help="Emit machine-readable JSON envelopes on stdout (instead of rich tables).",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", envvar="WALLET_QUIET",
        help="Suppress non-essential status lines (rich mode only; no-op under --json).",
    ),
    explain: bool = typer.Option(
        False, "--explain", envvar="WALLET_EXPLAIN",
        help="Print policy / idempotency decision details to stderr.",
    ),
) -> None:
    """Top-level options applied to every subcommand."""
    OutputMode.json = json_output
    OutputMode.quiet = quiet
    OutputMode.explain = explain


@app.command()
def version() -> None:
    """Print wallet CLI version."""
    emit(
        {"ok": True, "command": "version", "data": {"version": __version__}},
        render_rich=lambda d: stdout_console().print(
            f"wallet [bold cyan]{d['data']['version']}[/bold cyan]"
        ),
    )


@app.command()
def info() -> None:
    """Show config paths and default chain."""
    from wallet.core.config import get_chain
    from wallet.storage.state import state_path

    chain = get_chain("sepolia")
    data = {
        "ok": True,
        "command": "info",
        "chain": chain.name,
        "data": {
            "state_file": str(state_path()),
            "default_chain": chain.name,
            "chain_id": chain.chain_id,
            "rpc_url": chain.rpc_url,
            "explorer_tx_url": chain.explorer_tx_url,
        },
    }

    def render(d: dict) -> None:
        c = stdout_console()
        x = d["data"]
        c.print(f"state file:   [dim]{x['state_file']}[/dim]")
        c.print(f"default chain:[dim] {x['default_chain']} (chainId={x['chain_id']})[/dim]")
        c.print(f"rpc:          [dim]{x['rpc_url']}[/dim]")
        c.print(f"explorer:     [dim]{x['explorer_tx_url']}[/dim]")

    emit(data, render)


if __name__ == "__main__":
    app()

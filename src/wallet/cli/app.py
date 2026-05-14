from __future__ import annotations

import typer

from wallet import __version__
from wallet.cli import aave as aave_cli
from wallet.cli import account as account_cli
from wallet.cli import approve as approve_cli
from wallet.cli import book as book_cli
from wallet.cli import chain as chain_cli
from wallet.cli import policy as policy_cli
from wallet.cli import token as token_cli
from wallet.cli import watch as watch_cli
from wallet.cli._output import OutputMode, emit, stdout_console
from wallet.cli.balance import balance as balance_cmd
from wallet.cli.history import history as history_cmd
from wallet.cli.portfolio import portfolio as portfolio_cmd
from wallet.cli.send import send as send_cmd
from wallet.cli.swap import swap as swap_cmd

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
app.add_typer(aave_cli.app, name="aave")
app.add_typer(chain_cli.app, name="chain")
app.command("balance")(balance_cmd)
app.command("portfolio")(portfolio_cmd)
app.command("send")(send_cmd)
app.command("swap")(swap_cmd)
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
    debug: bool = typer.Option(
        False, "--debug", envvar="WALLET_DEBUG",
        help="Print verbose RPC request/response traces to stderr.",
    ),
) -> None:
    """Top-level options applied to every subcommand."""
    OutputMode.json = json_output
    OutputMode.quiet = quiet
    OutputMode.explain = explain
    OutputMode.debug = debug

    if debug:
        # Lightweight web3 + urllib3 request logging — the cheapest way to see
        # what the RPC actually got asked and what it answered. Suppressed when
        # the flag is off so normal runs stay clean.
        import logging
        import re

        # urllib3 logs the request line at DEBUG, which contains the full URL
        # path. When the wallet's chains.json embeds an Alchemy/Infura API key
        # in the path (`/v2/<KEY>`), every successful request would otherwise
        # write the key to stderr — and any agent harness that captures stderr
        # would pull the key into its context. Install a filter that scrubs
        # credential-shaped path segments + basic-auth userinfo before the
        # record reaches any handler.
        _KEY_IN_PATH = re.compile(r"(/v\d+/)([A-Za-z0-9_-]{20,})")
        _OPAQUE_PATH = re.compile(r"(/)([A-Za-z0-9_-]{32,})(/|$|\s|\")")
        _BASIC_AUTH = re.compile(r"(https?://)[^:/@\s]+:[^@\s]+@")

        class _CredentialScrubFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                try:
                    full = record.getMessage()
                except Exception:
                    return True
                scrubbed = _KEY_IN_PATH.sub(r"\1<redacted>", full)
                scrubbed = _OPAQUE_PATH.sub(r"\1<redacted>\3", scrubbed)
                scrubbed = _BASIC_AUTH.sub(r"\1<redacted>@", scrubbed)
                if scrubbed != full:
                    record.msg = scrubbed
                    record.args = None
                return True

        logging.basicConfig(level=logging.WARNING)
        scrub = _CredentialScrubFilter()
        for name in ("web3.providers.HTTPProvider", "web3.RequestManager", "urllib3.connectionpool"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.DEBUG)
            lg.addFilter(scrub)
        # Also install on the root logger so any other library that picks up
        # urllib3's record via propagation gets the scrubbed form too.
        logging.getLogger().addFilter(scrub)


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
def info(
    chain: str | None = typer.Option(None, "--chain", help="Inspect a specific chain (default: state.default_chain)"),
) -> None:
    """Show config paths and the resolved chain (default or `--chain`)."""
    from wallet.core.config import get_chain
    from wallet.core.rpc import redact_url
    from wallet.storage.state import load_state, state_path

    state = load_state()
    resolved_chain_name = chain or state.default_chain
    try:
        chain_cfg = get_chain(resolved_chain_name)
    except ValueError as e:
        from wallet.cli._output import emit_error
        emit_error("not_found", command="info", reason=str(e))
        raise typer.Exit(code=1)

    data = {
        "ok": True,
        "command": "info",
        "chain": chain_cfg.name,
        "data": {
            "state_file": str(state_path()),
            "default_chain": state.default_chain,
            "active_chain": chain_cfg.name,
            "chain_id": chain_cfg.chain_id,
            # Always redact — `wallet info` is the canonical "share config
            # with support" command and routinely gets pasted into chat.
            "rpc_url": redact_url(chain_cfg.rpc_url),
            "explorer_tx_url": chain_cfg.explorer_tx_url,
        },
    }

    def render(d: dict) -> None:
        c = stdout_console()
        x = d["data"]
        c.print(f"state file:   [dim]{x['state_file']}[/dim]")
        c.print(f"default chain:[dim] {x['default_chain']}[/dim]")
        c.print(f"active chain: [bold cyan]{x['active_chain']}[/bold cyan] (chainId={x['chain_id']})")
        c.print(f"rpc:          [dim]{x['rpc_url']}[/dim]")
        c.print(f"explorer:     [dim]{x['explorer_tx_url']}[/dim]")

    emit(data, render)


if __name__ == "__main__":
    app()

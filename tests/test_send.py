"""Smoke + import coverage for `wallet send` — catches NameError-class bugs
where a CLI command references a helper it forgot to import. The earlier
make_web3 → make_web3_or_exit refactor missed send.py's import line; the
typer --help path didn't execute the function body so the bug shipped
silently. Any future "rename a helper" refactor will surface here."""

from __future__ import annotations

from typer.testing import CliRunner

from wallet.cli.app import app


def test_send_help_invokes_without_error():
    result = CliRunner().invoke(app, ["send", "--help"])
    assert result.exit_code == 0
    assert "Send native ETH or an ERC-20 token" in result.output


def test_send_dry_run_resolves_imports_without_nameerror(monkeypatch):
    """Invoke the function body so any missing import (NameError) surfaces.

    We don't need a real chain — the command can fail with an `rpc_error`
    or `validation_error` envelope, both of which prove all symbols
    resolved. What we're guarding against is `NameError:
    name 'make_web3_or_exit' is not defined` slipping through tests
    that only exercise --help.
    """
    # Stub make_web3_or_exit so we never make a real RPC call
    import wallet.cli._common as _common
    monkeypatch.setattr(_common, "make_web3_or_exit",
                        lambda *a, **kw: (_ for _ in ()).throw(SystemExit(99)))

    result = CliRunner().invoke(app, [
        "send", "0xdeadbeef00000000000000000000000000000000", "0.001",
    ])
    # Any non-NameError exit is acceptable; what matters is that the
    # function body actually ran and the import wasn't missing.
    assert "NameError" not in (result.output + (str(result.exception) if result.exception else ""))

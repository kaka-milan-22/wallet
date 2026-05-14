"""Smoke + import coverage for `wallet send`.

Catches the NameError-class bug where a CLI command references a helper it
forgot to import. The earlier `make_web3` → `make_web3_or_exit` refactor
missed send.py's import line; the typer `--help` path doesn't execute the
function body so the bug shipped silently. Any future "rename a helper"
refactor should surface here.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from wallet.cli.app import app


def test_send_help_invokes_without_error():
    result = CliRunner().invoke(app, ["send", "--help"])
    assert result.exit_code == 0
    assert "Send native ETH or an ERC-20 token" in result.output


def test_send_function_body_runs_with_all_names_resolved(monkeypatch, tmp_path: Path):
    """The bug class we're guarding against is a missing `from … import …`
    line in `cli/send.py`. `--help` doesn't trigger it because the function
    body never executes; we have to actually enter the body.

    Strategy: patch the FIRST external helper send() reaches
    (`make_web3_or_exit`) to raise a sentinel `SystemExit(99)`. If every
    name in the body resolves, control reaches our stub and exits 99. If
    any helper is unbound, the function NameErrors before our stub fires
    and `result.exception` is a NameError instance.

    Critical detail: send.py imports the helper via
        `from wallet.cli._common import make_web3_or_exit`
    which binds the name into `wallet.cli.send`'s own namespace.
    Patching `wallet.cli._common.make_web3_or_exit` would NOT redirect that
    bound name — the original test did exactly this and was vacuous. The
    patch target has to be the *importing* module."""
    sentinel = SystemExit(99)

    def early_exit(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr("wallet.cli.send.make_web3_or_exit", early_exit)
    # Use a clean WALLET_HOME so the test doesn't depend on the user's real
    # state.json (which may or may not have a default account configured).
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))

    result = CliRunner().invoke(
        app,
        ["send", "0xdeadbeef00000000000000000000000000000000", "0.001"],
    )

    # The point of the test: if any import is missing the body never
    # reaches our stub and surfaces a NameError instead.
    assert not isinstance(result.exception, NameError), (
        f"NameError escaped from send() body — "
        f"a referenced name is not imported: {result.exception}"
    )

    # Belt + suspenders: confirm control actually reached our stub. If exit
    # is anything else (e.g. typer code 2 from BadParameter), something
    # short-circuited *before* the import we're trying to test — meaning
    # the test isn't actually exercising the bug class. Adjust the stub
    # target or the invocation, don't relax this assertion.
    assert result.exit_code == 99, (
        f"expected stub SystemExit(99) but got exit={result.exit_code}, "
        f"exception={result.exception!r}. Stub never fired — test is "
        f"vacuous."
    )


def test_approve_show_function_body_runs_with_all_names_resolved(
    monkeypatch, tmp_path: Path
):
    """Same bug class as test_send_function_body_runs above, but for
    `wallet approve show` — `approve.py:102` referenced `_sender`, a
    helper that was deleted in the Tier 2.2 sender-resolution refactor.
    `--help` and the `--owner` path both miss the line; only `approve
    show <token> --spender ...` without `--owner` triggers it.

    Stub `make_web3_or_exit` so the body enters but never talks to RPC.
    """
    sentinel = SystemExit(99)

    def early_exit(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr("wallet.cli.approve.make_web3_or_exit", early_exit)
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))

    result = CliRunner().invoke(
        app,
        [
            "approve", "show",
            "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "--spender", "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        ],
    )

    assert not isinstance(result.exception, NameError), (
        f"NameError escaped from approve.show body: {result.exception}"
    )

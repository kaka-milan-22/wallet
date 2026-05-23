"""Misplaced global flag hint: `wallet account list --json` should explain
that `--json` is a top-level option and tell the user to put it before the
subcommand. Tests the pure hint formatter directly so we don't have to
exercise the click standalone-mode wrapper from inside CliRunner."""

from __future__ import annotations

import pytest

from wallet.cli.app import _GLOBAL_FLAGS, _global_flag_hint


@pytest.mark.parametrize("flag", _GLOBAL_FLAGS)
def test_global_flag_hint_recognized_for_each_global_option(flag: str):
    hint = _global_flag_hint(f"No such option: {flag}")
    assert hint is not None
    assert flag in hint
    assert "BEFORE the subcommand" in hint
    assert f"wallet {flag}" in hint


def test_global_flag_hint_returns_none_for_subcommand_options():
    """A misplaced subcommand-only option (e.g. `--broadcast` without `send`)
    must not trigger the global-flag hint — the hint would suggest the
    wrong fix."""
    assert _global_flag_hint("No such option: --broadcast") is None
    assert _global_flag_hint("No such option: --slippage-bps") is None


def test_global_flag_hint_returns_none_for_other_usage_errors():
    """Unrelated UsageError messages (missing argument, bad value) get
    handled by click's default formatter — no hint suppression needed
    but also no hint emission."""
    assert _global_flag_hint("Missing argument 'TO'") is None
    assert _global_flag_hint("Invalid value for '--fee'") is None
    assert _global_flag_hint("") is None

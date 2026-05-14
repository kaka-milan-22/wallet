"""Single output channel for all wallet CLI commands.

Three global flags (set via top-level callback in `cli/app.py` or env var):

- `--json`  / WALLET_JSON=1     emit machine-readable JSON envelopes on stdout
- `--quiet` / WALLET_QUIET=1    suppress status lines (rich mode only)
- `--explain` / WALLET_EXPLAIN=1 print decision-trace details to stderr

Every command must build a `dict` of structured data and call
`emit(data, render_rich)`. The single helper picks the right channel:

- JSON mode → `json.dumps(data) + "\\n"` to stdout
- rich mode → call `render_rich(data)` to draw the table/panel

Errors go through `emit_error(code, **fields)` which uses the same envelope
in JSON mode and prints a colored line to **stderr** in rich mode.

Stdout is reserved for command data only — `info()` status lines also go to
stdout in rich mode but are dropped in JSON / quiet mode. `--explain` always
writes to stderr regardless of mode, so `wallet --json --explain ... | jq`
stays clean.
"""

from __future__ import annotations

import json as _json
import os
import sys
from typing import Any, Callable

from rich.console import Console


class OutputMode:
    """Module-level global state. Initialized from env vars at import time;
    overridden by `cli/app.py` callback when CLI flags are passed."""

    json: bool = os.environ.get("WALLET_JSON") == "1"
    quiet: bool = os.environ.get("WALLET_QUIET") == "1"
    explain: bool = os.environ.get("WALLET_EXPLAIN") == "1"
    # Verbose RPC tracing. Distinct from `explain` (policy/idempotency decisions):
    # `debug` dumps every HTTP request and response to/from the configured RPC,
    # which is what you actually need when "balance is wrong" or "swap router
    # quoter returned zeroes" — neither of those routes through `explain`.
    debug: bool = os.environ.get("WALLET_DEBUG") == "1"


# Construct Console lazily inside each call. Eagerly-cached `Console(file=sys.stdout)`
# would bind to the original stdout and miss pytest's capsys redirection.
def stdout_console() -> Console:
    return Console(file=sys.stdout)


def stderr_console() -> Console:
    return Console(file=sys.stderr)


def _write_json_line(obj: dict[str, Any]) -> None:
    sys.stdout.write(_json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n")
    sys.stdout.flush()


def emit(data: dict[str, Any], render_rich: Callable[[dict[str, Any]], None] | None = None) -> None:
    """Emit a successful result.

    JSON mode  → write `data` as a single-line JSON envelope to stdout
    Rich mode  → call `render_rich(data)` to draw human-readable output
    """
    if OutputMode.json:
        _write_json_line(data)
        return
    if render_rich is not None:
        render_rich(data)


def emit_error(
    code: str,
    *,
    command: str = "",
    chain: str = "",
    reason: str = "",
    **extra: Any,
) -> None:
    """Emit a structured error.

    JSON mode  → `{"ok": false, "error": code, "code": code, "reason": ..., ...}` on stdout
    Rich mode  → red `error: code — reason` line on **stderr**
    """
    if OutputMode.json:
        env: dict[str, Any] = {
            "ok": False,
            "command": command,
            "chain": chain,
            "error": code,
            "code": code,
            "reason": reason or code,
        }
        env.update(extra)
        _write_json_line(env)
    else:
        text = f"[red]error:[/red] {code}"
        if reason and reason != code:
            text += f" — {reason}"
        stderr_console().print(text)


def info(msg: str) -> None:
    """Status line. Suppressed in JSON or quiet mode; rich-only otherwise."""
    if OutputMode.json or OutputMode.quiet:
        return
    stdout_console().print(msg)


def explain(msg: str) -> None:
    """Decision-trace detail. Only emitted when --explain is on; always stderr
    (so JSON stdout pipelines remain clean)."""
    if not OutputMode.explain:
        return
    stderr_console().print(f"[dim]\\[explain][/dim] {msg}")


def debug(msg: str) -> None:
    """Verbose RPC / internals trace. Only emitted when --debug is on; always
    stderr (so JSON stdout pipelines remain clean). Cheap when disabled."""
    if not OutputMode.debug:
        return
    stderr_console().print(f"[dim]\\[debug][/dim] {msg}")


def reset_for_test() -> None:
    """Restore env-driven defaults — used by the test suite to isolate cases."""
    OutputMode.json = os.environ.get("WALLET_JSON") == "1"
    OutputMode.quiet = os.environ.get("WALLET_QUIET") == "1"
    OutputMode.explain = os.environ.get("WALLET_EXPLAIN") == "1"
    OutputMode.debug = os.environ.get("WALLET_DEBUG") == "1"

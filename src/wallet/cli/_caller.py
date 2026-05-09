"""Classify the wallet's caller as a human in a TTY or a programmatic agent.

`caller_kind()` is the single load-bearing primitive every defence layer
(policy / idempotency requirement / skill) consults. We deliberately do NOT
sniff for specific agents (Claude Code, Cursor, etc.) via env vars — the
isatty() check covers all of them uniformly: anything that runs wallet as a
subprocess (Bash from an LLM, shell pipe, cron, CI) gets classified as
`agent` and falls under the strict ruleset.
"""

from __future__ import annotations

import sys
from typing import Literal


def caller_kind() -> Literal["tty", "agent"]:
    """`tty` iff both stdin AND stdout are real terminals; otherwise `agent`."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        return "tty"
    return "agent"


def is_agent() -> bool:
    return caller_kind() == "agent"


def is_tty() -> bool:
    return caller_kind() == "tty"

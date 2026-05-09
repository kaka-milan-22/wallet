"""Append-only JSON-lines audit log of every broadcast attempt.

Path: `~/.wallet/audit.log` (alongside state.json).
Mode: 0644 (owner write, anyone read locally).
Format: one JSON object per line, terminated by `\n`.
Atomicity: O_APPEND ensures concurrent writes never interleave bytes.

This module deliberately exposes ONLY a writer. There is no `read()` /
`tail()` helper, and no CLI surface (`wallet audit` does not exist). An
agent can `cat` the file out-of-band, but the wallet does not advertise
the audit channel as a programmatic capability — keeping audit data out
of the agent's planning loop.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_data_dir


def audit_path() -> Path:
    base = Path(user_data_dir("wallet", appauthor=False))
    base.mkdir(parents=True, exist_ok=True)
    return base / "audit.log"


def now_iso() -> str:
    """Current UTC time as ISO-8601 with seconds precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write(event: dict) -> None:
    """Append one JSON-line event. O_APPEND guarantees atomic writes across
    concurrent processes/threads (the kernel serialises append writes that
    fit within PIPE_BUF = 512 bytes; we stay well under that)."""
    if "ts" not in event:
        event = {"ts": now_iso(), **event}

    line = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
    data = line.encode()

    fd = os.open(
        audit_path(),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)

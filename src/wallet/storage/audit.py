"""Append-only JSON-lines audit log of every broadcast attempt.

Path: `~/.wallet/audit.log` (alongside state.json).
Mode: 0600 (owner read+write only). Earlier versions used 0644 — when an
older log is detected, this module migrates it to 0600 on first write.
Format: one JSON object per line, terminated by `\n`.
Atomicity: O_APPEND ensures concurrent writes never interleave bytes.

This module deliberately exposes ONLY a writer. There is no `read()` /
`tail()` helper, and no CLI surface (`wallet audit` does not exist). An
agent can `cat` the file out-of-band, but the wallet does not advertise
the audit channel as a programmatic capability — keeping audit data out
of the agent's planning loop. The tightened 0600 mode also stops other
same-host UIDs from reading the trail when /home is shared.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from wallet.core.config import data_root

__all__ = ["audit_path", "now_iso", "write"]

_AUDIT_FILE_MODE = 0o600


def audit_path() -> Path:
    base = data_root()
    base.mkdir(parents=True, exist_ok=True)
    return base / "audit.log"


def _enforce_mode(p: Path) -> None:
    """Tighten an existing audit file from older 0o644 to 0o600. No-op for
    files already at 0o600 (or stricter)."""
    try:
        current = os.stat(p).st_mode & 0o777
    except OSError:
        return
    if current != _AUDIT_FILE_MODE:
        try:
            os.chmod(p, _AUDIT_FILE_MODE)
        except OSError:
            pass


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

    p = audit_path()
    fd = os.open(
        p,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        _AUDIT_FILE_MODE,
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    # `os.open` only sets mode on file CREATION; existing files keep their
    # old mode. Migrate older 0o644 files in place after every write so
    # the security upgrade lands without requiring a manual chmod.
    _enforce_mode(p)

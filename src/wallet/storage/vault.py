"""Wrapper around the `agent-vault` CLI.

Only the agent-safe subcommands are exposed: `has`, `list`, `read`, `write`.
The sensitive ones (`set`, `get`, `rm`, `import`) are never invoked from this
process — they require a human in a TTY and would fail anyway.

To get a secret into Python memory we use a one-shot temp file:

    agent-vault write <tmp> --content "<agent-vault:key>"

`write` substitutes the placeholder with the real value on disk. We read the
file back, then unlink. The secret lives on disk only for microseconds and the
file is created with 0600 by `mkstemp`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile


class VaultError(RuntimeError):
    pass


def _bin() -> str:
    p = shutil.which("agent-vault")
    if not p:
        raise VaultError(
            "agent-vault not found on PATH. Install: npm i -g @botiverse/agent-vault"
        )
    return p


def has(key: str) -> bool:
    """Return True iff `key` exists in the vault."""
    r = subprocess.run(
        [_bin(), "has", "--json", key],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return False
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False
    if isinstance(data, dict):
        return bool(data.get(key, False))
    return False


def reveal(key: str) -> str:
    """Return the plaintext secret stored under `key`.

    Caller must treat the returned string as sensitive and avoid writing it to
    logs / stdout / files. The string lives only in this Python process memory.
    """
    if not has(key):
        raise VaultError(f"vault key not found: {key}")

    fd, path = tempfile.mkstemp(prefix="wallet-secret-", text=True)
    os.close(fd)
    try:
        placeholder = f"<agent-vault:{key}>"
        r = subprocess.run(
            [_bin(), "write", path, "--content", placeholder],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            raise VaultError(f"agent-vault write failed: {r.stderr.strip() or r.stdout.strip()}")
        with open(path) as f:
            value = f.read()
        if value == placeholder:
            raise VaultError(
                f"placeholder was not substituted — key '{key}' may be missing or unreadable"
            )
        return value.rstrip("\n")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

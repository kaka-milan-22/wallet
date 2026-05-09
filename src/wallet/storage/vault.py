"""Wrapper around the `agent-vault` CLI.

Only the agent-safe subcommands are exposed: `has`, `list`, `read`, `write`.
The sensitive ones (`set`, `get`, `rm`, `import`) are never invoked from this
process — they require a human in a TTY and would fail anyway.

To get a secret into Python memory we use a Unix named pipe (FIFO):

    agent-vault write <fifo> --content "<agent-vault:key>"

`mkfifo` creates only an inode — no data ever touches disk. agent-vault
substitutes the placeholder with the real value inside its own process and
writes the bytes through the kernel pipe buffer, where this process reads
them. A 0600 FIFO inode in a 0700 temp directory keeps other UIDs out.

The reader uses `O_NONBLOCK` + `select.select` so a crashed / hung writer
times out cleanly instead of pinning the wallet process forever.

A legacy temp-file path (`_reveal_via_tempfile`) is kept as a fallback for
platforms where the FIFO transfer fails (timeout / OSError / agent-vault
incompatibility). It preserves Phase 1 behaviour and is no worse than the
prior baseline.
"""

from __future__ import annotations

import errno
import json
import os
import select
import shutil
import subprocess
import tempfile
import time

# How long to wait for `agent-vault write` to substitute and stream bytes
# through the FIFO before we give up and fall back to the tempfile path.
_FIFO_TIMEOUT_SECONDS = 10


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
    logs / stdout / files. It lives only in this Python process memory.

    Transport: kernel FIFO between agent-vault and this process. Falls back
    to a 0600 temp file if the FIFO path fails on this platform.
    """
    if not has(key):
        raise VaultError(f"vault key not found: {key}")

    fifo_dir = tempfile.mkdtemp(prefix="wallet-vault-")  # 0700
    fifo = os.path.join(fifo_dir, "p")
    placeholder = f"<agent-vault:{key}>"

    proc: subprocess.Popen | None = None
    try:
        os.mkfifo(fifo, mode=0o600)

        proc = subprocess.Popen(
            [_bin(), "write", fifo, "--content", placeholder],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        value = _read_fifo_with_timeout(fifo, _FIFO_TIMEOUT_SECONDS)

        rc = proc.wait(timeout=_FIFO_TIMEOUT_SECONDS)
        if rc != 0:
            err = (proc.stderr.read() if proc.stderr else "").strip()
            raise VaultError(f"agent-vault write failed: {err or f'exit {rc}'}")
        if value == placeholder:
            raise VaultError(
                f"placeholder was not substituted — key '{key}' may be missing or unreadable"
            )
        return value.rstrip("\n")
    except (subprocess.TimeoutExpired, OSError, BrokenPipeError):
        # FIFO transport is best-effort. On any platform-level failure (timeout
        # waiting for writer, agent-vault refuses FIFO sinks, etc.), kill the
        # writer (if still alive) and degrade to the legacy tempfile path,
        # which is identical to Phase 1 behaviour — never worse than baseline.
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        return _reveal_via_tempfile(key)
    finally:
        try:
            os.unlink(fifo)
        except OSError:
            pass
        try:
            os.rmdir(fifo_dir)
        except OSError:
            pass


def _read_fifo_with_timeout(path: str, timeout: float) -> str:
    """Read until EOF from a FIFO with a wall-clock timeout.

    Opens with O_NONBLOCK so a never-connecting writer doesn't pin us, then
    uses select() to wait for data. Raises subprocess.TimeoutExpired if
    nothing arrives or the writer never closes within `timeout` seconds.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd="agent-vault write", timeout=timeout)
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise subprocess.TimeoutExpired(cmd="agent-vault write", timeout=timeout)
            try:
                chunk = os.read(fd, 4096)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                raise
            if not chunk:
                # All writers closed → EOF.
                break
            chunks.append(chunk)
        return b"".join(chunks).decode()
    finally:
        os.close(fd)


def _reveal_via_tempfile(key: str) -> str:
    """Legacy fallback: agent-vault writes to a 0600 temp file, we read + unlink.

    The plaintext exists on disk for milliseconds. Used only if the FIFO
    transport in `reveal()` fails on this platform.
    """
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
            raise VaultError(
                f"agent-vault write failed: {r.stderr.strip() or r.stdout.strip()}"
            )
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

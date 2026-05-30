"""Wrapper around the `alice` CLI (the AnB secrets client).

Only the agent-safe subcommands are exposed: `has` and `write`. The sensitive
ones (`set`, `get`, `rm`, `import`) are never invoked from this process — they
require a human in a TTY and would fail anyway.

AnB is the client/server successor to agent-vault. `alice` keeps only
ciphertext on disk; the master key lives in `bob`, a separate KMS daemon that
`alice` reaches over mutual TLS. The redaction model and placeholder format are
unchanged: secrets are referenced as `<agent-vault:key>` and restored by
`alice write`.

    alice write <fifo> --content "<agent-vault:key>" --quiet

`mkfifo` creates only an inode — no data ever touches disk. alice asks bob to
decrypt the value inside its own process and writes the bytes through the
kernel pipe buffer, where this process reads them. Status lines go to stderr
(silenced with `--quiet`), so only the restored secret reaches the FIFO. A
0600 FIFO inode in a 0700 temp directory keeps other UIDs out.

The reader uses `O_NONBLOCK` + `select.select` so a crashed / hung writer
times out cleanly instead of pinning the wallet process forever. It also polls
the writer process: if `alice` exits before producing output (the common case
when **bob is unreachable or locked**), we bail immediately instead of waiting
out the full timeout.

A legacy temp-file path (`_reveal_via_tempfile`) is kept as a fallback for
platforms where the FIFO transfer fails (timeout / OSError / alice
incompatibility). The plaintext exists on disk for milliseconds there.

Runtime dependency: unlike agent-vault (purely local), `reveal()` needs `bob`
running and unlocked — every decrypt is an mTLS round-trip. `has()` reads only
local metadata and works while bob is down. Errors that look like an
unreachable or locked bob are surfaced with a remediation hint rather than a
generic failure.
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

# How long to wait for `alice write` to substitute and stream bytes through the
# FIFO before we give up and fall back to the tempfile path.
_FIFO_TIMEOUT_SECONDS = 10

# Poll the writer process this often (seconds) while waiting on the FIFO, so a
# bob-down `alice` that exits without writing is detected promptly.
_POLL_SLICE_SECONDS = 0.25


class VaultError(RuntimeError):
    pass


class VaultUnavailableError(VaultError):
    """bob is unreachable or locked — a transient, operator-fixable condition,
    distinct from a missing key or a substitution failure."""


def _bin() -> str:
    p = shutil.which("alice")
    if not p:
        raise VaultError(
            "alice not found on PATH. Install: "
            "go install github.com/kaka-milan-22/AnB/v3/cmd/alice@latest"
        )
    return p


def _alice_write_cmd(sink: str, placeholder: str, reason: str | None) -> list[str]:
    """Build `alice write` argv, optionally with the v2.4+ `--reason` flag.

    Centralised so the FIFO path and the tempfile fallback can't drift apart:
    if one acquires a new flag, the other inherits it for free. Reason strings
    are passed verbatim — alice does its own length / charset enforcement.
    """
    cmd = [_bin(), "write", sink, "--content", placeholder, "--quiet"]
    if reason:
        cmd += ["--reason", reason]
    return cmd


def _looks_unavailable(stderr: str) -> bool:
    """Heuristic: does this alice stderr indicate bob is down or locked rather
    than a key/substitution problem? Used only to pick a clearer error class."""
    s = stderr.lower()
    needles = (
        "connection refused",
        "connect: ",
        "dial tcp",
        "no route to host",
        "i/o timeout",
        "context deadline exceeded",
        "locked",
        "unlock",
        "bob status",
        "tls",
        "handshake",
        "eof",
    )
    return any(n in s for n in needles)


def _fail(stderr: str, rc: int) -> VaultError:
    """Map an `alice write` failure to the right error class."""
    msg = stderr.strip() or f"exit {rc}"
    if _looks_unavailable(stderr):
        return VaultUnavailableError(
            f"bob appears unreachable or locked ({msg}). "
            f"Start/unlock it with `bob serve` and check `alice status`."
        )
    return VaultError(f"alice write failed: {msg}")


def has(key: str) -> bool:
    """Return True iff `key` exists in the vault.

    Reads local metadata only — no bob round-trip — so it works while bob is
    down. (That is why callers must still be ready for `reveal()` to fail on an
    unreachable bob even after `has()` returns True.)
    """
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


def reveal(key: str, *, reason: str | None = None) -> str:
    """Return the plaintext secret stored under `key`.

    Caller must treat the returned string as sensitive and avoid writing it to
    logs / stdout / files. It lives only in this Python process memory.

    `reason` (AnB v2.4+): a short free-text "why" string forwarded to alice via
    `--reason R`. Bob's JSON audit log records it on the ALLOW line, which lets
    `bob.log` correlate every mnemonic reveal with the wallet operation that
    needed it (request_id, tx description, etc.) — independent of wallet's own
    audit log, so the two can be cross-referenced after the fact.

    Transport: kernel FIFO between alice and this process; alice asks bob to
    decrypt. Falls back to a 0600 temp file if the FIFO path fails on this
    platform. Raises VaultUnavailableError if bob is unreachable / locked.
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
            _alice_write_cmd(fifo, placeholder, reason),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        value = _read_fifo_with_timeout(fifo, _FIFO_TIMEOUT_SECONDS, proc)

        rc = proc.wait(timeout=_FIFO_TIMEOUT_SECONDS)
        if rc != 0:
            raise _fail(proc.stderr.read() if proc.stderr else "", rc)
        if value == placeholder:
            raise VaultError(
                f"placeholder was not substituted — key '{key}' may be missing or unreadable"
            )
        return value.rstrip("\n")
    except (subprocess.TimeoutExpired, OSError, BrokenPipeError):
        # FIFO transport is best-effort. On any platform-level failure (timeout
        # waiting for writer, alice refuses FIFO sinks, alice exited before
        # writing because bob is down, etc.), kill the writer (if still alive)
        # and degrade to the legacy tempfile path, which re-runs alice and
        # surfaces bob's own error if the problem is bob, not the FIFO.
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        return _reveal_via_tempfile(key, reason=reason)
    finally:
        try:
            os.unlink(fifo)
        except OSError:
            pass
        try:
            os.rmdir(fifo_dir)
        except OSError:
            pass


def _read_fifo_with_timeout(
    path: str, timeout: float, proc: subprocess.Popen | None = None
) -> str:
    """Read until EOF from a FIFO with a wall-clock timeout.

    Opens with O_NONBLOCK so a never-connecting writer doesn't pin us, then
    uses select() to wait for data. If `proc` is given and exits before any
    bytes arrive, we raise BrokenPipeError immediately (bob-down fast path)
    instead of burning the whole timeout. Raises subprocess.TimeoutExpired if
    nothing arrives or the writer never closes within `timeout` seconds.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd="alice write", timeout=timeout)
            ready, _, _ = select.select([fd], [], [], min(remaining, _POLL_SLICE_SECONDS))
            if not ready:
                # No data yet. If the writer process has already exited without
                # ever connecting, there is nothing left to wait for — fail fast
                # so the caller can fall back / surface bob's error.
                if not chunks and proc is not None and proc.poll() is not None:
                    raise BrokenPipeError("alice exited before producing output")
                continue
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


def _reveal_via_tempfile(key: str, *, reason: str | None = None) -> str:
    """Legacy fallback: alice writes to a 0600 temp file, we read + unlink.

    The plaintext exists on disk for milliseconds. Used only if the FIFO
    transport in `reveal()` fails on this platform. Also the terminal path when
    bob is down — alice's stderr is classified into VaultUnavailableError here.

    `reason` is threaded through to alice's `--reason` flag exactly as in the
    FIFO path, so the audit story stays consistent across transports.
    """
    fd, path = tempfile.mkstemp(prefix="wallet-secret-", text=True)
    os.close(fd)
    try:
        placeholder = f"<agent-vault:{key}>"
        r = subprocess.run(
            _alice_write_cmd(path, placeholder, reason),
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            raise _fail(r.stderr or r.stdout, r.returncode)
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

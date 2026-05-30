"""Tests for storage.vault.reveal()'s FIFO transport + tempfile fallback.

We mock `subprocess.Popen` and `subprocess.run` (so the test does not depend on
alice being installed or the user's actual vault contents) but use a real FIFO
and real `select.select` to exercise the production transport code.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from unittest.mock import MagicMock

import pytest

from wallet.storage import vault
from wallet.storage.vault import VaultError, VaultUnavailableError, reveal


class FakeAlice:
    """Simulates `alice write <fifo> --content <placeholder> --quiet` by writing
    `write_value` to the FIFO in a background thread.

    `exited=True` makes `poll()` report the exit code *before* `wait()` is
    called — used to model an alice that died immediately (e.g. bob down) so the
    reader's fast-bail path can be exercised. `stderr_text` overrides what
    `proc.stderr.read()` returns."""

    def __init__(
        self,
        fifo_path: str,
        write_value: str | None,
        *,
        exit_code: int = 0,
        stderr_text: str | None = None,
        exited: bool = False,
    ):
        self.fifo = fifo_path
        self.write_value = write_value
        self.exit_code = exit_code
        self.returncode: int | None = exit_code if exited else None
        self.stderr = MagicMock()
        default_err = "" if exit_code == 0 else f"fake exit {exit_code}"
        self.stderr.read = MagicMock(
            return_value=stderr_text if stderr_text is not None else default_err
        )
        self._thread: threading.Thread | None = None
        if write_value is not None:
            self._thread = threading.Thread(target=self._writer, daemon=True)
            self._thread.start()

    def _writer(self) -> None:
        try:
            # tiny delay so the wallet's open() blocks first, exercising the
            # real reader-blocked-on-writer FIFO sequence
            time.sleep(0.02)
            with open(self.fifo, "w") as f:
                f.write(self.write_value or "")
        except OSError:
            pass

    def wait(self, timeout: float | None = None) -> int:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise subprocess.TimeoutExpired(cmd="alice", timeout=timeout)
        self.returncode = self.exit_code
        return self.exit_code

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        # No-op; the daemon thread is allowed to finish on its own.
        pass


def _popen_factory(write_value: str | None, exit_code: int = 0):
    def factory(args, **kwargs):
        # args = [bin, "write", fifo_path, "--content", placeholder, "--quiet"]
        return FakeAlice(args[2], write_value, exit_code=exit_code)

    return factory


@pytest.fixture
def has_true(monkeypatch):
    monkeypatch.setattr(vault, "has", lambda key: True)


@pytest.fixture
def has_false(monkeypatch):
    monkeypatch.setattr(vault, "has", lambda key: False)


def test_reveal_returns_substituted_value(has_true, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _popen_factory("real-mnemonic-content"))
    assert reveal("k") == "real-mnemonic-content"


def test_reveal_strips_trailing_newline(has_true, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _popen_factory("mnemonic\n"))
    assert reveal("k") == "mnemonic"


def test_reveal_raises_when_placeholder_unchanged(has_true, monkeypatch):
    # alice wrote the placeholder back unchanged — substitution failed
    monkeypatch.setattr(subprocess, "Popen", _popen_factory("<agent-vault:k>"))
    with pytest.raises(VaultError, match="placeholder was not substituted"):
        reveal("k")


def test_reveal_raises_when_key_missing(has_false):
    with pytest.raises(VaultError, match="vault key not found"):
        reveal("missing-key")


def test_reveal_raises_on_nonzero_exit(has_true, monkeypatch):
    # Empty write + rc=1 → VaultError("alice write failed: ...")
    monkeypatch.setattr(subprocess, "Popen", _popen_factory("", exit_code=1))
    with pytest.raises(VaultError, match="alice write failed"):
        reveal("k")


def test_reveal_classifies_bob_down(has_true, monkeypatch):
    """When alice exits immediately without writing (bob unreachable), the
    reader fast-bails, the tempfile fallback re-runs alice, and the
    connection-refused stderr is classified as VaultUnavailableError — not a
    generic failure."""
    monkeypatch.setattr(vault, "_FIFO_TIMEOUT_SECONDS", 0.3)

    # Popen: alice exited 1 right away, never connected a writer to the FIFO.
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda args, **kw: FakeAlice(args[2], None, exit_code=1, exited=True),
    )

    def fake_run(args, **kwargs):
        r = MagicMock()
        r.returncode = 1
        r.stderr = "dial tcp 127.0.0.1:8443: connect: connection refused"
        r.stdout = ""
        return r

    monkeypatch.setattr(subprocess, "run", fake_run)

    started = time.monotonic()
    with pytest.raises(VaultUnavailableError, match="unreachable or locked"):
        reveal("k")
    # Fast-bail: must NOT have waited out the full FIFO timeout.
    assert time.monotonic() - started < 0.3


def test_reveal_cleans_fifo_on_success(has_true, monkeypatch):
    captured: dict[str, str] = {}
    real_mkdtemp = tempfile.mkdtemp

    def spy(*a, **kw):
        d = real_mkdtemp(*a, **kw)
        captured["dir"] = d
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    monkeypatch.setattr(subprocess, "Popen", _popen_factory("ok"))

    reveal("k")
    assert captured["dir"]
    assert not os.path.exists(captured["dir"]), f"fifo dir still exists: {captured['dir']}"


def test_reveal_cleans_fifo_on_error(has_true, monkeypatch):
    captured: dict[str, str] = {}
    real_mkdtemp = tempfile.mkdtemp

    def spy(*a, **kw):
        d = real_mkdtemp(*a, **kw)
        captured["dir"] = d
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    monkeypatch.setattr(subprocess, "Popen", _popen_factory("<agent-vault:k>"))

    with pytest.raises(VaultError):
        reveal("k")
    assert not os.path.exists(captured["dir"])


def test_reveal_forwards_reason_to_alice(has_true, monkeypatch):
    """v1.11: reveal(key, reason='...') must add `--reason <reason>` to the
    alice subprocess call so Bob's audit log captures why the mnemonic was
    decrypted. Regression for the audit-correlation feature."""
    captured: dict[str, list[str]] = {}

    def factory(args, **_kw):
        captured["args"] = list(args)
        return FakeAlice(args[2], "real-mnemonic")

    monkeypatch.setattr(subprocess, "Popen", factory)
    assert reveal("k", reason="send 0.01 ETH on sepolia (req=abc)") == "real-mnemonic"

    # `--reason` must appear in the alice argv, with the exact reason string.
    args = captured["args"]
    assert "--reason" in args, f"missing --reason in {args}"
    idx = args.index("--reason")
    assert args[idx + 1] == "send 0.01 ETH on sepolia (req=abc)", args[idx + 1]


def test_reveal_omits_reason_when_none(has_true, monkeypatch):
    """No reason given → no --reason flag in argv. Keeps the surface clean for
    callers that don't care about audit correlation (and for backward compat
    with operators on pre-v2.4 alice)."""
    captured: dict[str, list[str]] = {}

    def factory(args, **_kw):
        captured["args"] = list(args)
        return FakeAlice(args[2], "x")

    monkeypatch.setattr(subprocess, "Popen", factory)
    reveal("k")  # no reason passed
    assert "--reason" not in captured["args"], captured["args"]


def test_reveal_falls_back_to_tempfile_on_writer_hang(has_true, monkeypatch):
    """If the FIFO writer never connects within the timeout, the wallet must
    degrade gracefully to the legacy tempfile path. This proves the fallback
    is wired correctly."""

    # Speed up the timeout for a fast test
    monkeypatch.setattr(vault, "_FIFO_TIMEOUT_SECONDS", 0.3)

    # write_value=None ⇒ no writer thread spawned ⇒ FIFO never gets a writer.
    # poll() stays None (proc still "running"), so the fast-bail path does not
    # fire and we exercise the real timeout → fallback transition.
    monkeypatch.setattr(subprocess, "Popen", _popen_factory(None))

    real_mnemonic = "fallback-mnemonic-recovered"

    def fake_run(args, **kwargs):
        # Simulate alice write to the tempfile path: substitute on disk.
        path = args[2]
        with open(path, "w") as f:
            f.write(real_mnemonic)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        result.stdout = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)

    started = time.monotonic()
    value = reveal("k")
    elapsed = time.monotonic() - started

    assert value == real_mnemonic
    # Confirm we actually waited for the timeout (not just bailed instantly)
    assert elapsed >= 0.3

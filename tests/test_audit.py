"""Audit log writer — format, permissions, atomicity, no CLI exposure."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import stat
from pathlib import Path

import pytest

from wallet.storage import audit


@pytest.fixture
def isolated_audit(tmp_path: Path, monkeypatch):
    p = tmp_path / "audit.log"
    monkeypatch.setattr(audit, "audit_path", lambda: p)
    return p


def test_write_creates_file_with_0600(isolated_audit):
    """New audit logs are owner-only (0600) — readable only by the wallet user."""
    audit.write({"chain": "sepolia", "kind": "send", "outcome": "broadcast"})
    mode = stat.S_IMODE(os.stat(isolated_audit).st_mode)
    assert mode == 0o600, f"audit log mode should be 0600, got {oct(mode)}"


def test_write_migrates_older_0o644_log(isolated_audit, tmp_path):
    """If a previous version wrote audit.log at 0o644, the next write should
    tighten it to 0o600 in place (no manual chmod required)."""
    # Pre-seed a file at 0o644 mimicking the old version
    isolated_audit.write_text('{"ts":"2026-01-01T00:00:00Z","kind":"send"}\n')
    os.chmod(isolated_audit, 0o644)
    assert stat.S_IMODE(os.stat(isolated_audit).st_mode) == 0o644

    audit.write({"chain": "sepolia", "kind": "send", "outcome": "broadcast"})

    mode = stat.S_IMODE(os.stat(isolated_audit).st_mode)
    assert mode == 0o600, f"expected migration to 0o600, got {oct(mode)}"


def test_write_emits_valid_jsonl(isolated_audit):
    audit.write({"kind": "send", "to": "0xabc", "amount_wei": "1000"})
    audit.write({"kind": "approve", "spender": "0xdef", "amount_wei": "0"})
    lines = isolated_audit.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert "ts" in obj
        assert "kind" in obj


def test_write_auto_adds_timestamp(isolated_audit):
    audit.write({"kind": "send"})
    obj = json.loads(isolated_audit.read_text().strip())
    # ISO-8601 UTC: YYYY-MM-DDTHH:MM:SSZ
    assert obj["ts"].endswith("Z")
    assert "T" in obj["ts"]


def test_write_preserves_caller_provided_ts(isolated_audit):
    audit.write({"ts": "2020-01-01T00:00:00Z", "kind": "send"})
    obj = json.loads(isolated_audit.read_text().strip())
    assert obj["ts"] == "2020-01-01T00:00:00Z"


def _writer_proc(audit_path_str: str, marker: str) -> None:
    """Run in a child process; write 50 lines as fast as possible."""
    # Re-import inside the child so `audit_path` rebind via monkeypatch is lost;
    # we have to re-monkeypatch via env so use a simple direct write here.
    path = Path(audit_path_str)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        for i in range(50):
            line = json.dumps({"marker": marker, "i": i}, separators=(",", ":")) + "\n"
            os.write(fd, line.encode())
    finally:
        os.close(fd)


def test_concurrent_appends_never_interleave(tmp_path: Path):
    """O_APPEND must keep each line atomic across multiple processes."""
    p = tmp_path / "audit.log"

    procs = [
        mp.Process(target=_writer_proc, args=(str(p), f"writer-{i}"))
        for i in range(4)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=10)
        assert proc.exitcode == 0

    lines = p.read_text().splitlines()
    assert len(lines) == 4 * 50
    # Every line must round-trip as JSON — no torn writes
    for line in lines:
        obj = json.loads(line)
        assert obj["marker"].startswith("writer-")
        assert isinstance(obj["i"], int)


def test_audit_subcommand_not_exposed_in_cli():
    """Defensive check: wallet --help must not list an `audit` command.
    The whole point of the audit log is that the agent does not get a
    programmatic read channel."""
    from typer.testing import CliRunner

    from wallet.cli.app import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    # match boundary forms only — "auditing" or similar is fine, "audit" alone is not
    lower = result.output.lower()
    assert "│ audit " not in lower, "wallet --help unexpectedly exposes an `audit` command"
    assert " audit " not in lower or "auditor" in lower or "audit log" in lower

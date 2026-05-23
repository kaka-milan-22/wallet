"""Tier 2.5 — atomic_write_text durability + permissions contract.

We can't directly test 'survives crash mid-write' without a real kernel
fault-injection harness, but we can verify the file is owner-only, that
the tmp file never lingers, and that the result on disk is exactly what
we asked to write (not partial)."""

from __future__ import annotations

import os
import stat
from pathlib import Path


from wallet.core.config import atomic_write_text


def test_atomic_write_creates_file_with_0600(tmp_path: Path):
    p = tmp_path / "state.json"
    atomic_write_text(p, '{"foo":"bar"}')
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_atomic_write_overwrites_existing(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text("OLD CONTENT THAT SHOULD BE GONE")
    atomic_write_text(p, "NEW")
    assert p.read_text() == "NEW"


def test_atomic_write_cleans_up_tmp_file(tmp_path: Path):
    p = tmp_path / "state.json"
    atomic_write_text(p, "x")
    tmp = p.with_suffix(p.suffix + ".tmp")
    assert not tmp.exists(), "intermediate .tmp file should have been renamed away"


def test_atomic_write_creates_parent_directory(tmp_path: Path):
    p = tmp_path / "newdir" / "newer" / "state.json"
    assert not p.parent.exists()
    atomic_write_text(p, "x")
    assert p.exists()


def test_atomic_write_unicode_roundtrip(tmp_path: Path):
    p = tmp_path / "state.json"
    payload = '{"name": "测试", "emoji": "🌚"}'
    atomic_write_text(p, payload)
    assert p.read_text(encoding="utf-8") == payload


def test_atomic_write_tightens_mode_on_existing_loose_file(tmp_path: Path):
    """If somebody previously created the file at 0644, the next atomic write
    must end at 0600 — important because policy / state are sensitive."""
    p = tmp_path / "state.json"
    p.write_text("old")
    os.chmod(p, 0o644)
    atomic_write_text(p, "new")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600

"""Output channel: emit / emit_error / info / explain across modes."""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

from wallet.cli import _output
from wallet.cli._output import OutputMode, emit, emit_error, explain, info


@pytest.fixture(autouse=True)
def reset_mode(monkeypatch):
    monkeypatch.delenv("WALLET_JSON", raising=False)
    monkeypatch.delenv("WALLET_QUIET", raising=False)
    monkeypatch.delenv("WALLET_EXPLAIN", raising=False)
    OutputMode.json = False
    OutputMode.quiet = False
    OutputMode.explain = False
    yield


def _capture_stdout():
    buf = io.StringIO()
    sys.stdout = buf
    return buf


def _capture_stderr():
    buf = io.StringIO()
    # rich Console captures via a different file handle, so use the helper
    # built into our module
    return buf


def test_emit_json_mode_writes_one_line(capsys):
    OutputMode.json = True
    emit({"ok": True, "command": "test", "data": {"value": 42}})
    out = capsys.readouterr().out
    assert out.endswith("\n")
    obj = json.loads(out)
    assert obj == {"ok": True, "command": "test", "data": {"value": 42}}


def test_emit_rich_mode_calls_render(capsys):
    called: dict = {}
    def render(d):
        called["data"] = d
    emit({"command": "x", "data": {"k": 1}}, render)
    assert called["data"] == {"command": "x", "data": {"k": 1}}


def test_emit_rich_mode_no_render_is_silent(capsys):
    emit({"x": 1})
    out = capsys.readouterr().out
    assert out == ""


def test_emit_error_json_mode_envelope(capsys):
    OutputMode.json = True
    emit_error("policy_block", command="send", chain="sepolia",
               reason="max-per-tx-exceeded:ETH:0.001")
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["ok"] is False
    assert obj["error"] == "policy_block"
    assert obj["code"] == "policy_block"
    assert obj["command"] == "send"
    assert obj["chain"] == "sepolia"
    assert obj["reason"] == "max-per-tx-exceeded:ETH:0.001"


def test_emit_error_rich_mode_writes_stderr(capsys):
    emit_error("validation_error", reason="bad amount")
    captured = capsys.readouterr()
    # rich rendering may add ANSI codes but plain text "validation_error" must appear
    assert "validation_error" in captured.err
    # MUST NOT pollute stdout (so jq pipelines stay clean even under errors)
    assert captured.out == ""


def test_emit_error_extra_fields_passthrough(capsys):
    OutputMode.json = True
    emit_error("idempotency_mismatch", request_id="abc-123", original_hash="0xff")
    obj = json.loads(capsys.readouterr().out)
    assert obj["request_id"] == "abc-123"
    assert obj["original_hash"] == "0xff"


def test_info_emits_in_rich_mode(capsys):
    info("hello status")
    assert "hello status" in capsys.readouterr().out


def test_info_silent_in_json_mode(capsys):
    OutputMode.json = True
    info("noisy status")
    assert capsys.readouterr().out == ""


def test_info_silent_in_quiet_mode(capsys):
    OutputMode.quiet = True
    info("quiet me")
    assert capsys.readouterr().out == ""


def test_explain_silent_when_flag_off(capsys):
    explain("policy decision: allow")
    captured = capsys.readouterr()
    assert "explain" not in captured.out
    assert "explain" not in captured.err


def test_explain_writes_stderr_when_on(capsys):
    OutputMode.explain = True
    explain("policy decision: allow")
    captured = capsys.readouterr()
    assert "policy decision: allow" in captured.err
    # NEVER pollute stdout — jq pipeline must stay clean
    assert captured.out == ""


def test_explain_stderr_even_in_json_mode(capsys):
    OutputMode.json = True
    OutputMode.explain = True
    explain("policy: deny no-policy-configured")
    captured = capsys.readouterr()
    assert "policy: deny" in captured.err
    assert captured.out == ""


def test_envvar_initializes_mode(monkeypatch):
    monkeypatch.setenv("WALLET_JSON", "1")
    monkeypatch.setenv("WALLET_QUIET", "1")
    monkeypatch.setenv("WALLET_EXPLAIN", "1")
    _output.reset_for_test()
    assert OutputMode.json is True
    assert OutputMode.quiet is True
    assert OutputMode.explain is True

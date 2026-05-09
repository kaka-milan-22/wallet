"""Caller classification — TTY vs agent."""

from unittest.mock import patch

from wallet.cli._caller import caller_kind, is_agent, is_tty


def test_caller_tty_when_both_stdin_stdout_isatty():
    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdout.isatty", return_value=True):
        assert caller_kind() == "tty"
        assert is_tty()
        assert not is_agent()


def test_caller_agent_when_stdout_piped():
    with patch("sys.stdin.isatty", return_value=True), \
         patch("sys.stdout.isatty", return_value=False):
        assert caller_kind() == "agent"


def test_caller_agent_when_stdin_piped():
    with patch("sys.stdin.isatty", return_value=False), \
         patch("sys.stdout.isatty", return_value=True):
        assert caller_kind() == "agent"


def test_caller_agent_when_neither_isatty():
    with patch("sys.stdin.isatty", return_value=False), \
         patch("sys.stdout.isatty", return_value=False):
        assert caller_kind() == "agent"
        assert is_agent()
        assert not is_tty()

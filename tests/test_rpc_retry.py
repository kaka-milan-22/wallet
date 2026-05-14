"""Tier 2.7 — call_with_retry backoff/retry behaviour."""

from __future__ import annotations

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, Timeout

from wallet.core.rpc import call_with_retry


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> HTTPError:
    return HTTPError(response=_FakeResponse(status))


def test_returns_value_when_first_attempt_succeeds():
    assert call_with_retry(lambda: 7) == 7


def test_retries_on_503_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(503)
        return "ok"

    out = call_with_retry(fn, base_delay=0, max_delay=0, sleep=lambda *_: None)
    assert out == "ok"
    assert calls["n"] == 3


def test_exhausts_and_reraises_last_error():
    def fn():
        raise _http_error(502)

    with pytest.raises(HTTPError):
        call_with_retry(fn, attempts=3, base_delay=0, max_delay=0, sleep=lambda *_: None)


def test_does_not_retry_on_4xx_non_429():
    """A 400 from a malformed eth_call is permanent — retrying just wastes
    quota."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _http_error(400)

    with pytest.raises(HTTPError):
        call_with_retry(fn, attempts=3, base_delay=0, sleep=lambda *_: None)
    assert calls["n"] == 1


def test_retries_on_429_rate_limit():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429)
        return "ok"

    out = call_with_retry(fn, attempts=3, base_delay=0, sleep=lambda *_: None)
    assert out == "ok"
    assert calls["n"] == 2


def test_retries_on_connection_error_and_timeout():
    seq = iter([RequestsConnectionError("net flap"), Timeout("slow"), "ok"])

    def fn():
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    out = call_with_retry(fn, attempts=3, base_delay=0, sleep=lambda *_: None)
    assert out == "ok"


def test_env_var_can_disable_retries(monkeypatch):
    monkeypatch.setenv("WALLET_RPC_RETRY_ATTEMPTS", "1")
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _http_error(503)

    with pytest.raises(HTTPError):
        call_with_retry(fn, attempts=5, base_delay=0, sleep=lambda *_: None)
    assert calls["n"] == 1, "env override should cap at one attempt"


def test_on_retry_callback_fires_with_index_and_exception():
    notes = []

    def fn():
        if not notes:
            return None  # never raises, so we need to force one retry first
        return "ok"

    # Force two failures then success
    seq = iter([_http_error(502), _http_error(503), "ok"])

    def flaky():
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    call_with_retry(
        flaky,
        attempts=3, base_delay=0,
        on_retry=lambda i, e: notes.append((i, type(e).__name__)),
        sleep=lambda *_: None,
    )
    assert notes == [(1, "HTTPError"), (2, "HTTPError")]


def test_sleep_uses_exponential_backoff():
    delays = []
    seq = iter([_http_error(502), _http_error(502), _http_error(502), "ok"])

    def fn():
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    call_with_retry(
        fn, attempts=4, base_delay=0.1, max_delay=10.0,
        sleep=lambda d: delays.append(d),
    )
    # 0.1, 0.2, 0.4
    assert delays == [pytest.approx(0.1), pytest.approx(0.2), pytest.approx(0.4)]

"""URL credential redaction.

RPC URLs often embed billable API keys (Alchemy / Infura / BlockPi /
QuickNode style). Any time the wallet emits a URL into a JSON envelope —
`wallet --json info`, `chain list`, `chain show`, `rpc_error.reason` —
the key would otherwise enter the consuming agent's context. Same
severity class as a mnemonic leak. `redact_url` is the single boundary
that scrubs those URLs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wallet.core.rpc import redact_url


# --- core helper --------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected_contains_not, expected_form_contains",
    [
        # Alchemy: 32-char alphanumeric key in /v2/
        (
            "https://eth-mainnet.g.alchemy.com/v2/abcdef1234567890ABCDEF1234567890",
            "abcdef1234567890ABCDEF1234567890",
            "/v2/<redacted>",
        ),
        # Infura: 32-char hex in /v3/
        (
            "https://mainnet.infura.io/v3/a1b2c3d4e5f6071829304a5b6c7d8e9f",
            "a1b2c3d4e5f6071829304a5b6c7d8e9f",
            "/v3/<redacted>",
        ),
        # BlockPi: key as last path segment
        (
            "https://ethereum.blockpi.network/v1/rpc/abcdefghijklmnopqrstuvwxyz0123",
            "abcdefghijklmnopqrstuvwxyz0123",
            "/v1/rpc/<redacted>",
        ),
        # Basic auth
        (
            "https://api_user:hunter2_secret_pw@rpc.example.com/eth",
            "hunter2_secret_pw",
            "<redacted>@rpc.example.com",
        ),
        # Query string with apikey
        (
            "https://rpc.example.com/eth?apikey=verysecretkey123456789012",
            "verysecretkey123456789012",
            "apikey=%3Credacted%3E",  # urlencoded form of <redacted>
        ),
        # Query string with access_token
        (
            "https://rpc.example.com/?access_token=secret-token-xyz-1234",
            "secret-token-xyz-1234",
            "access_token=",
        ),
    ],
)
def test_redacts_known_credential_shapes(raw, expected_contains_not, expected_form_contains):
    out = redact_url(raw)
    assert expected_contains_not not in out, (
        f"credential leaked through: {out}"
    )
    assert expected_form_contains in out, (
        f"redacted form doesn't preserve structure: {out}"
    )


@pytest.mark.parametrize(
    "raw",
    [
        # Free public RPCs — no credential to redact, round-trip unchanged
        "https://ethereum-sepolia.publicnode.com",
        "https://ethereum.llamarpc.com",
        "https://eth.drpc.org",
        "https://ankr.com/eth",
        "https://1rpc.io/eth",
        # Bare host
        "https://rpc.example.com",
        # Trailing slash
        "https://rpc.example.com/",
    ],
)
def test_keyless_urls_round_trip_unchanged(raw):
    assert redact_url(raw) == raw, (
        f"redaction altered a credential-free URL: {raw} → {redact_url(raw)}"
    )


def test_short_path_segments_are_not_redacted():
    """`v2`, `v3`, `rpc`, `eth` are real path components, not API keys."""
    url = "https://rpc.example.com/v2/rpc/eth"
    assert redact_url(url) == url


def test_empty_url_is_safe():
    assert redact_url("") == ""


def test_unparseable_url_returns_safe_placeholder():
    # urlsplit handles almost anything, but feed it something truly broken
    out = redact_url("\x00")
    # We get back either the original or a sentinel — what matters is no exception
    assert isinstance(out, str)


def test_wss_and_ipc_schemes_work():
    """Non-HTTP RPC transports should round-trip path redaction too."""
    out = redact_url("wss://eth.example.com/ws/v2/abcdefghijklmnop1234567890")
    assert "abcdefghijklmnop1234567890" not in out
    assert out.startswith("wss://")


# --- integration: JSON envelopes never leak the raw URL -----------------


def _seed_chain(tmp_path: Path, name: str, rpc_url: str) -> None:
    (tmp_path / "chains.json").write_text(json.dumps({
        name: {
            "name": name,
            "chain_id": 12345,
            "rpc_url": rpc_url,
            "explorer_api_url": "https://api.example.com",
            "explorer_tx_url": "https://example.com/tx/{tx}",
            "native_symbol": "ETH",
        }
    }))
    state = {
        "default_chain": name,
        "accounts": [], "book": {}, "watch": [], "tokens": [],
    }
    (tmp_path / "state.json").write_text(json.dumps(state))


def test_info_json_envelope_emits_redacted_rpc_url(tmp_path: Path, monkeypatch):
    """`wallet --json info` is the canonical "share config" command. Its
    JSON envelope must NOT contain a raw API key."""
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _seed_chain(tmp_path, "alch", "https://eth-sepolia.g.alchemy.com/v2/SECRET_KEY_abcdefghij1234567890")

    from wallet.cli.app import app
    result = CliRunner().invoke(app, ["--json", "info"])
    assert result.exit_code == 0, result.output
    obj = json.loads(result.output.strip())
    assert "SECRET_KEY_abcdefghij1234567890" not in result.output, (
        "raw API key leaked into JSON envelope: " + result.output
    )
    assert "<redacted>" in obj["data"]["rpc_url"]


def test_chain_show_json_envelope_emits_redacted_rpc_url(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    chains = {
        "myL1": {
            "name": "myL1",
            "chain_id": 1234,
            "rpc_url": "https://rpc.example.com/v3/leaky_api_token_abcdefghijklmnop",
            "explorer_api_url": "https://api.example.com",
            "explorer_tx_url": "https://example.com/tx/{tx}",
            "native_symbol": "ETH",
        }
    }
    (tmp_path / "chains.json").write_text(json.dumps(chains))

    from wallet.cli.app import app
    result = CliRunner().invoke(app, ["--json", "chain", "show", "myL1"])
    assert result.exit_code == 0, result.output
    assert "leaky_api_token_abcdefghijklmnop" not in result.output


def test_chain_list_json_envelope_emits_redacted_rpc_url(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    chains = {
        "myL2": {
            "name": "myL2",
            "chain_id": 5678,
            "rpc_url": "https://eth-mainnet.g.alchemy.com/v2/A_VERY_LONG_API_KEY_SECRET_123",
            "explorer_api_url": "https://api.example.com",
            "explorer_tx_url": "https://example.com/tx/{tx}",
            "native_symbol": "ETH",
        }
    }
    (tmp_path / "chains.json").write_text(json.dumps(chains))

    from wallet.cli.app import app
    result = CliRunner().invoke(app, ["--json", "chain", "list"])
    assert result.exit_code == 0, result.output
    assert "A_VERY_LONG_API_KEY_SECRET_123" not in result.output


def test_rpc_error_message_redacts_url():
    """RpcConnectError gets caught by every CLI command and emitted as the
    `reason` field of an `rpc_error` envelope. That envelope must not
    contain a raw URL credential."""
    from wallet.core.config import ChainConfig
    from wallet.core.rpc import make_web3, RpcConnectError

    # An obviously-unreachable host with a key in the path
    cfg = ChainConfig(
        name="testchain", chain_id=999,
        rpc_url="https://nonexistent.example.invalid/v2/MY_SECRET_API_KEY_xyz1234567",
        explorer_api_url="http://x", explorer_tx_url="http://x/{tx}",
        native_symbol="ETH",
    )
    with pytest.raises(RpcConnectError) as ei:
        make_web3(cfg, timeout=1)
    assert "MY_SECRET_API_KEY_xyz1234567" not in str(ei.value), (
        f"API key leaked into RpcConnectError: {ei.value}"
    )


# --- --debug logging must not leak credentials --------------------------


def test_debug_logging_scrubs_url_credentials(capsys, monkeypatch, tmp_path: Path):
    """`--debug` enables verbose urllib3 logging. urllib3 emits the full
    request path including any embedded API key. The filter installed by
    the --debug code path must scrub credential-shaped path segments
    before they cross any handler."""
    import logging
    import re

    # Replay the same filter logic the CLI installs
    _KEY_IN_PATH = re.compile(r"(/v\d+/)([A-Za-z0-9_-]{20,})")
    _OPAQUE_PATH = re.compile(r"(/)([A-Za-z0-9_-]{32,})(/|$|\s|\")")
    _BASIC_AUTH = re.compile(r"(https?://)[^:/@\s]+:[^@\s]+@")

    class _Filter(logging.Filter):
        def filter(self, record):
            try:
                full = record.getMessage()
            except Exception:
                return True
            scrubbed = _KEY_IN_PATH.sub(r"\1<redacted>", full)
            scrubbed = _OPAQUE_PATH.sub(r"\1<redacted>\3", scrubbed)
            scrubbed = _BASIC_AUTH.sub(r"\1<redacted>@", scrubbed)
            if scrubbed != full:
                record.msg = scrubbed
                record.args = None
            return True

    import io
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.DEBUG)
    h.addFilter(_Filter())
    lg = logging.getLogger("urllib3.connectionpool")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        # Mimic urllib3's actual log call shape
        lg.debug(
            '%s://%s:%s "%s %s HTTP/%s" %s %s',
            "https", "eth-mainnet.g.alchemy.com", 443,
            "POST", "/v2/SECRET_KEY_abcdefghij1234567890", "1.1",
            200, "1234",
        )
        lg.debug(
            "Resetting dropped connection: %s",
            "https://user_x:hunter2_password@rpc.example.com/eth",
        )
    finally:
        lg.removeHandler(h)

    captured = buf.getvalue()
    assert "SECRET_KEY_abcdefghij1234567890" not in captured, (
        f"API key in /v2/ leaked through filter: {captured}"
    )
    assert "hunter2_password" not in captured, (
        f"basic-auth password leaked through filter: {captured}"
    )
    assert "<redacted>" in captured


# --- explorer (Etherscan v2) must not leak its apikey query param -------


def test_explorer_http_error_does_not_leak_apikey_in_message(monkeypatch):
    """`httpx.HTTPStatusError.__str__` and `RequestError.__str__` both
    include the full request URL, which contains `apikey=<KEY>` for
    Etherscan v2 calls. Explorer._call must wrap them so the URL doesn't
    bubble up into the `rpc_error.reason` envelope that history.py emits."""
    import httpx
    from wallet.services import explorer
    from wallet.core.config import ChainConfig

    monkeypatch.setenv("ETHERSCAN_API_KEY", "MY_SECRET_ETHERSCAN_KEY_xyz_001")

    cfg = ChainConfig(
        name="sepolia", chain_id=11155111,
        rpc_url="https://x", explorer_api_url="https://api.etherscan.io/v2/api",
        explorer_tx_url="https://etherscan.io/tx/{tx}", native_symbol="ETH",
    )

    # Mock httpx.get to raise an HTTPStatusError whose URL includes the key
    class FakeResponse:
        status_code = 401
        text = "Invalid API Key"
    class FakeRequest:
        url = "https://api.etherscan.io/v2/api?chainid=11155111&apikey=MY_SECRET_ETHERSCAN_KEY_xyz_001"

    def fake_get(url, params=None, timeout=None):
        r = FakeResponse()
        r.request = FakeRequest()
        # Build the exception the way httpx would
        err = httpx.HTTPStatusError("401 Unauthorized", request=FakeRequest(), response=r)
        raise err

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(explorer.EtherscanError) as ei:
        explorer.list_native_txs(cfg, "0x" + "11" * 20)

    msg = str(ei.value)
    assert "MY_SECRET_ETHERSCAN_KEY_xyz_001" not in msg, (
        f"apikey leaked through EtherscanError: {msg}"
    )
    # Confirm __cause__ is None — `from None` suppression
    assert ei.value.__cause__ is None

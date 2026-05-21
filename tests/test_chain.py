"""wallet chain list / show — surface chains from both presets and chains.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wallet.cli.app import app
from wallet.core import config as config_mod


@pytest.fixture
def isolated_chains(tmp_path: Path, monkeypatch):
    """Redirect chains.json + state.json to a per-test temp dir."""
    chains_p = tmp_path / "chains.json"
    state_p = tmp_path / "state.json"
    monkeypatch.setattr(config_mod, "chains_config_path", lambda: chains_p)
    from wallet.storage import state as state_mod
    monkeypatch.setattr(state_mod, "state_path", lambda: state_p)
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)
    return tmp_path


def test_chain_list_shows_builtin_when_no_user_overrides(isolated_chains):
    runner = CliRunner()
    result = runner.invoke(app, ["chain", "list"])
    assert result.exit_code == 0, result.output
    assert "sepolia" in result.output
    assert "builtin" in result.output


def test_chain_list_includes_user_added(isolated_chains):
    (isolated_chains / "chains.json").write_text(json.dumps({
        "ethereum": {
            "name": "ethereum",
            "chain_id": 1,
            "rpc_url": "https://eth.drpc.org",
            "explorer_api_url": "https://api.etherscan.io/v2/api",
            "explorer_tx_url": "https://etherscan.io/tx/{tx}",
            "native_symbol": "ETH",
        }
    }))
    runner = CliRunner()
    result = runner.invoke(app, ["chain", "list"])
    assert result.exit_code == 0
    assert "ethereum" in result.output
    assert "user-added" in result.output


def test_chain_list_marks_user_override(isolated_chains):
    """When chains.json has a name that exists in builtins, it's marked as override."""
    (isolated_chains / "chains.json").write_text(json.dumps({
        "sepolia": {
            "name": "sepolia",
            "chain_id": 11155111,
            "rpc_url": "https://custom-sepolia.example.com",
            "explorer_api_url": "https://api.etherscan.io/v2/api",
            "explorer_tx_url": "https://sepolia.etherscan.io/tx/{tx}",
            "native_symbol": "ETH",
        }
    }))
    runner = CliRunner()
    result = runner.invoke(app, ["chain", "list"])
    assert result.exit_code == 0
    assert "user-override" in result.output


def test_chain_show_unknown_chain_emits_not_found(isolated_chains):
    runner = CliRunner()
    result = runner.invoke(app, ["chain", "show", "neverexisting"])
    assert result.exit_code == 1
    # Rich error goes to stderr; verify behavior via JSON envelope instead
    result_json = runner.invoke(app, ["--json", "chain", "show", "neverexisting"])
    assert result_json.exit_code == 1
    parsed = json.loads(result_json.output.strip().split("\n")[-1])
    assert parsed["ok"] is False
    assert parsed["error"] == "not_found"


def test_chain_show_sepolia_includes_protocols(isolated_chains):
    runner = CliRunner()
    result = runner.invoke(app, ["--json", "chain", "show", "sepolia"])
    assert result.exit_code == 0
    parsed = json.loads(result.output.strip().split("\n")[-1])
    assert parsed["ok"] is True
    assert parsed["data"]["chain_id"] == 11155111
    assert "uniswap_v3" in parsed["data"]["protocols"]
    assert "aave_v3" in parsed["data"]["protocols"]


# --- broadcast_rpc_url + mev_exposure plumbing -------------------------------


def test_chainconfig_defaults_mev_exposure_true_fail_closed():
    """A new chain config with neither field set defaults to mev_exposure=True
    (fail-closed). The policy gate then requires an explicit broadcast_rpc_url
    before any broadcast on that chain."""
    cfg = config_mod.ChainConfig(
        name="custom",
        chain_id=12345,
        rpc_url="https://rpc.example.invalid",
        explorer_api_url="https://api.example/v2/api",
        explorer_tx_url="https://example/tx/{tx}",
        native_symbol="ETH",
    )
    assert cfg.mev_exposure is True
    assert cfg.broadcast_rpc_url is None


def test_chainconfig_round_trips_broadcast_rpc_url_and_mev_exposure():
    cfg = config_mod.ChainConfig(
        name="ethereum",
        chain_id=1,
        rpc_url="https://eth.llamarpc.com",
        broadcast_rpc_url="https://rpc.flashbots.net",
        mev_exposure=True,
        explorer_api_url="https://api.etherscan.io/v2/api",
        explorer_tx_url="https://etherscan.io/tx/{tx}",
        native_symbol="ETH",
    )
    blob = cfg.model_dump_json()
    parsed = config_mod.ChainConfig.model_validate_json(blob)
    assert parsed.broadcast_rpc_url == "https://rpc.flashbots.net"
    assert parsed.mev_exposure is True


def test_builtin_sepolia_explicitly_opts_out_of_mev_exposure(isolated_chains):
    """Sepolia preset must declare mev_exposure=False so the policy gate
    stays quiet on testnet without operator action."""
    chain = config_mod.get_chain("sepolia")
    assert chain.mev_exposure is False
    assert chain.broadcast_rpc_url is None


def test_user_chains_json_can_set_broadcast_rpc_url(isolated_chains):
    (isolated_chains / "chains.json").write_text(json.dumps({
        "ethereum": {
            "name": "ethereum",
            "chain_id": 1,
            "rpc_url": "https://eth.llamarpc.com",
            "broadcast_rpc_url": "https://rpc.flashbots.net",
            "explorer_api_url": "https://api.etherscan.io/v2/api",
            "explorer_tx_url": "https://etherscan.io/tx/{tx}",
            "native_symbol": "ETH",
        }
    }))
    chain = config_mod.get_chain("ethereum")
    assert chain.broadcast_rpc_url == "https://rpc.flashbots.net"
    # User chain configs default to mev_exposure=True (Pydantic schema default).
    assert chain.mev_exposure is True

"""CLI integration tests for `wallet lp`.

Strategy mirrors `tests/test_send.py`: invoke through `CliRunner`, redirect
`WALLET_HOME` to a tmp dir, monkey-patch the web3 boundary at the importing
module (`wallet.cli.lp.make_web3_or_exit`). For end-to-end paths that
exercise dry-run JSON envelopes and the idempotency replay flag we wire up
a full mocked w3.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner
from web3 import Web3

from wallet.cli.app import app
from wallet.core.tokens import clear_token_info_cache


NFPM_ADDR = "0x1238536071E1c677A632429e3655c799b22cDA52"
FACTORY_ADDR = "0x0227628f3F023bb0B980b67D528571c95c6DaC1c"
USDC_ADDR = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
WETH_ADDR = "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14"
POOL_ADDR = "0x4444444444444444444444444444444444444444"
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def _write_state(tmp_path: Path) -> None:
    state = {
        "default_account": "alice",
        "default_chain": "sepolia",
        "accounts": [{
            "name": "alice",
            "address": SENDER,
            "derivation_path": "m/44'/60'/0'/0/0",
            "vault_key": "stub",
        }],
        "book": {}, "watch": [], "tokens": [],
    }
    (tmp_path / "state.json").write_text(json.dumps(state))


def _write_policy(tmp_path: Path, *, allow_nfpm: bool = True) -> None:
    policy = {
        "max_per_tx": {},
        "max_per_day": {},
        "recipient_allowlist": [],
        "contract_allowlist": [NFPM_ADDR] if allow_nfpm else [],
        "deny_unlimited_approve": True,
        "first_send_warn": False,
        "sentinel_blocklist": [],
        "min_health_factor": None,
    }
    (tmp_path / "policy.json").write_text(json.dumps(policy))


@pytest.fixture(autouse=True)
def _wipe_token_cache():
    clear_token_info_cache()
    yield
    clear_token_info_cache()


# --- help-text smoke (catches NameError-class import bugs) -----------------


@pytest.mark.parametrize("subcmd", ["positions", "collect", "remove", "mint", "increase"])
def test_lp_subcommand_help_resolves(subcmd):
    r = CliRunner().invoke(app, ["lp", subcmd, "--help"])
    assert r.exit_code == 0, r.output


def test_lp_group_help_resolves():
    r = CliRunner().invoke(app, ["lp", "--help"])
    assert r.exit_code == 0
    assert "positions" in r.output
    assert "mint" in r.output


def test_lp_command_body_runs_with_all_names_resolved(monkeypatch, tmp_path: Path):
    """NameError guard — same shape as tests/test_send.py.

    Stub the first external helper each command body reaches
    (`make_web3_or_exit`); if any imported name is unbound the body
    NameErrors before the stub fires.
    """
    sentinel = SystemExit(99)

    def early_exit(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr("wallet.cli.lp.make_web3_or_exit", early_exit)
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)

    for argv in (
        ["lp", "positions"],
        ["lp", "collect", "1"],
        ["lp", "remove", "1", "--percent", "100"],
        ["lp", "mint", "USDC", "WETH",
         "--fee", "3000", "--tick-lower", "-60", "--tick-upper", "60",
         "--amount-a", "1", "--amount-b", "1"],
        ["lp", "increase", "1", "USDC", "WETH",
         "--amount-a", "1", "--amount-b", "1"],
    ):
        r = CliRunner().invoke(app, argv)
        assert not isinstance(r.exception, NameError), (
            f"{argv[1]} body unbound name: {r.exception}"
        )
        assert r.exit_code == 99, (
            f"{argv[1]}: expected stub exit 99 but got {r.exit_code}; "
            f"exception={r.exception!r}"
        )


# --- mocked end-to-end paths ----------------------------------------------


_DEFAULT_SLOT0 = [2**96, 0, 0, 0, 0, 0, True]


def _positions_tuple(
    *,
    token0=USDC_ADDR,
    token1=WETH_ADDR,
    fee=3000,
    tick_lower=-60,
    tick_upper=60,
    liquidity=10**18,
    owed0=0,
    owed1=0,
):
    return [
        0, "0x0000000000000000000000000000000000000000",
        Web3.to_checksum_address(token0),
        Web3.to_checksum_address(token1),
        fee, tick_lower, tick_upper, liquidity, 0, 0, owed0, owed1,
    ]


class _MockSpec:
    def __init__(self) -> None:
        self.balance_of: dict[str, int] = {}
        self.token_owner_index: dict[tuple[str, int], int] = {}
        self.positions_tuple = _positions_tuple()
        self.slot0_tuple = list(_DEFAULT_SLOT0)
        self.pool_address = POOL_ADDR
        self.allowances: dict[tuple[str, str, str], int] = {}
        self.token_meta: dict[str, tuple[str, int]] = {
            USDC_ADDR.lower(): ("USDC", 6),
            WETH_ADDR.lower(): ("WETH", 18),
        }
        self.estimate_gas_value = 250_000
        self.simulate_raises: Exception | None = None


def _make_mock_w3(spec: _MockSpec):
    w3 = MagicMock()

    def wrap(value):
        inner = MagicMock()
        inner.call = lambda: value
        return inner

    def contract_factory(address: str, abi: list[dict]):
        addr_l = address.lower()
        c = MagicMock()
        if addr_l == NFPM_ADDR.lower():
            c.functions.balanceOf = lambda owner: wrap(spec.balance_of.get(owner.lower(), 0))
            c.functions.tokenOfOwnerByIndex = lambda owner, idx: wrap(
                spec.token_owner_index[(owner.lower(), idx)]
            )
            c.functions.positions = lambda token_id: wrap(spec.positions_tuple)

            def make_action(name):
                def wrapper(_params):
                    inner = MagicMock()
                    def build_tx(base: dict):
                        return {
                            **base,
                            "to": Web3.to_checksum_address(NFPM_ADDR),
                            "data": "0x" + name.encode().hex(),
                            "value": 0,
                            "gas": spec.estimate_gas_value,
                            "nonce": 0,
                        }
                    inner.build_transaction = build_tx
                    return inner
                return wrapper
            c.functions.collect = make_action("collect")
            c.functions.decreaseLiquidity = make_action("decreaseLiquidity")
            c.functions.increaseLiquidity = make_action("increaseLiquidity")
            c.functions.mint = make_action("mint")
            c.encode_abi = lambda name, args: "0x" + name.encode().hex() + "00" * 32
        elif addr_l == FACTORY_ADDR.lower():
            c.functions.getPool = lambda *_a: wrap(spec.pool_address)
        elif addr_l == spec.pool_address.lower():
            c.functions.slot0 = lambda: wrap(spec.slot0_tuple)
        else:
            meta = spec.token_meta.get(addr_l, ("???", 18))
            c.functions.symbol = lambda: wrap(meta[0])
            c.functions.decimals = lambda: wrap(meta[1])
            c.functions.allowance = lambda owner, spender: wrap(
                spec.allowances.get((addr_l, owner.lower(), spender.lower()), 0)
            )
        return c

    w3.eth.contract = contract_factory
    w3.eth.chain_id = 11155111
    w3.eth.max_priority_fee = 10**9
    w3.eth.get_block = lambda *_: {"baseFeePerGas": 2 * 10**9}
    w3.eth.estimate_gas = lambda _tx: spec.estimate_gas_value
    def eth_call(_tx):
        if spec.simulate_raises is not None:
            raise spec.simulate_raises
        return b""
    w3.eth.call = eth_call
    return w3


def _install_mock(monkeypatch, tmp_path: Path, spec: _MockSpec) -> None:
    """Common harness: redirect WALLET_HOME, write state + policy, patch w3."""
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)
    _write_policy(tmp_path, allow_nfpm=True)

    w3 = _make_mock_w3(spec)
    monkeypatch.setattr("wallet.cli.lp.make_web3_or_exit", lambda cfg, command: w3)


def test_positions_command_returns_json_envelope(monkeypatch, tmp_path: Path):
    spec = _MockSpec()
    spec.balance_of[SENDER.lower()] = 1
    spec.token_owner_index[(SENDER.lower(), 0)] = 7
    _install_mock(monkeypatch, tmp_path, spec)

    r = CliRunner().invoke(app, ["--json", "lp", "positions"])
    assert r.exit_code == 0, r.output
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["ok"] is True
    assert env["command"] == "lp.positions"
    assert len(env["data"]["positions"]) == 1
    row = env["data"]["positions"][0]
    assert row["token_id"] == 7
    assert row["in_range"] is True
    assert row["pair"] == "USDC/WETH"


def test_mint_dry_run_emits_preview_envelope(monkeypatch, tmp_path: Path):
    spec = _MockSpec()
    spec.allowances[(USDC_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**12
    spec.allowances[(WETH_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    _install_mock(monkeypatch, tmp_path, spec)

    r = CliRunner().invoke(app, [
        "--json", "lp", "mint", "USDC", "WETH",
        "--fee", "3000", "--tick-lower", "-60", "--tick-upper", "60",
        "--amount-a", "1000", "--amount-b", "1",
        "--dry-run",
    ])
    assert r.exit_code == 0, r.output
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["ok"] is True
    assert env["command"] == "lp_mint"
    d = env["data"]
    assert d["phase"] == "preview"
    assert d["lp_action"] == "mint"
    assert d["lp_fee"] == 3000
    assert d["lp_tick_lower"] == -60
    assert d["lp_tick_upper"] == 60


def test_mint_insufficient_allowance_emits_error_envelope(monkeypatch, tmp_path: Path):
    spec = _MockSpec()
    # No allowance for USDC.
    spec.allowances[(WETH_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    _install_mock(monkeypatch, tmp_path, spec)

    r = CliRunner().invoke(app, [
        "--json", "lp", "mint", "USDC", "WETH",
        "--fee", "3000", "--tick-lower", "-60", "--tick-upper", "60",
        "--amount-a", "1000", "--amount-b", "1",
        "--dry-run",
    ])
    assert r.exit_code == 2
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["ok"] is False
    assert env["error"] == "insufficient_allowance"
    assert env["data"]["token_symbol"] == "USDC"
    assert env["data"]["suggested_command"].startswith("wallet approve set USDC")


def test_mint_misaligned_tick_emits_validation_error(monkeypatch, tmp_path: Path):
    spec = _MockSpec()
    spec.allowances[(USDC_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**12
    spec.allowances[(WETH_ADDR.lower(), SENDER.lower(), NFPM_ADDR.lower())] = 10**20
    _install_mock(monkeypatch, tmp_path, spec)

    r = CliRunner().invoke(app, [
        "--json", "lp", "mint", "USDC", "WETH",
        "--fee", "3000", "--tick-lower", "-55", "--tick-upper", "60",
        "--amount-a", "1000", "--amount-b", "1",
        "--dry-run",
    ])
    assert r.exit_code == 2
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["error"] == "validation_error"
    assert "not aligned" in env["reason"]


def test_remove_dry_run_envelope(monkeypatch, tmp_path: Path):
    spec = _MockSpec()
    _install_mock(monkeypatch, tmp_path, spec)

    r = CliRunner().invoke(app, [
        "--json", "lp", "remove", "1", "--percent", "100", "--dry-run",
    ])
    assert r.exit_code == 0, r.output
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["command"] == "lp_decrease"
    assert env["data"]["lp_percent"] == 100.0


def test_collect_policy_block_when_nfpm_missing_from_allowlist(
    monkeypatch, tmp_path: Path
):
    """Confirms the new policy branch fires for collect (any LP op should
    block when NFPM is absent from contract_allowlist)."""
    spec = _MockSpec()
    monkeypatch.setenv("WALLET_HOME", str(tmp_path))
    _write_state(tmp_path)
    _write_policy(tmp_path, allow_nfpm=False)
    w3 = _make_mock_w3(spec)
    monkeypatch.setattr("wallet.cli.lp.make_web3_or_exit", lambda cfg, command: w3)

    r = CliRunner().invoke(app, [
        "--json", "lp", "collect", "1", "--broadcast", "--yes",
    ])
    assert r.exit_code == 3
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["error"] == "policy_block"
    assert "lp-nfpm-not-in-contract-allowlist" in env["reason"]


# --- lp mint slippage-revert enrichment -------------------------------------


def test_mint_slippage_revert_surfaces_expected_amounts(monkeypatch, tmp_path: Path):
    """When prepare_mint reverts with `Price slippage check` (the dominant
    failure mode when the agent's amount_a / amount_b don't match the
    pool's current ratio), the error envelope must include expected
    `(amount0, amount1)` and a suggestion pointing at the binding side."""
    from web3.exceptions import ContractLogicError

    spec = _MockSpec()
    _install_mock(monkeypatch, tmp_path, spec)

    def raise_slippage(*a, **kw):
        raise ContractLogicError("execution reverted: Price slippage check")

    monkeypatch.setattr("wallet.cli.lp.prepare_mint", raise_slippage)
    monkeypatch.setattr(
        "wallet.cli.lp.compute_mint_expected_amounts",
        lambda *a, **kw: {
            "lp_pool_address": "0x" + "5" * 40,
            "lp_current_sqrt_price_x96": "12345",
            "lp_current_tick": 187575,
            "lp_token0_symbol": "USDC",
            "lp_token0_address": USDC_ADDR,
            "lp_token0_decimals": 6,
            "lp_token1_symbol": "WETH",
            "lp_token1_address": WETH_ADDR,
            "lp_token1_decimals": 18,
            "lp_amount0_desired_wei": "10000000",
            "lp_amount1_desired_wei": "1600000000000000",
            "lp_amount0_expected_wei": "10000000",
            "lp_amount1_expected_wei": "1413000000000000",
            "lp_binding_side": "token0",
        },
    )

    r = CliRunner().invoke(app, [
        "--json", "lp", "mint", "USDC", "WETH",
        "--fee", "500", "--tick-lower", "187500", "--tick-upper", "187650",
        "--amount-a", "10", "--amount-b", "0.0016",
        "--slippage-bps", "200",
    ])
    assert r.exit_code == 3
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["error"] == "simulation_reverted"
    assert "Price slippage check" in env["reason"]
    d = env["data"]
    assert d["lp_amount0_expected_wei"] == "10000000"
    assert d["lp_amount1_expected_wei"] == "1413000000000000"
    assert d["lp_binding_side"] == "token0"
    assert d["lp_current_tick"] == 187575
    # token_a=USDC=token0 → over-funded side is token1=WETH → user adjusts --amount-b
    assert "--amount-b" in d["suggestion"]
    assert "WETH" in d["suggestion"]


def test_mint_non_slippage_revert_skips_enrichment(monkeypatch, tmp_path: Path):
    """Non-slippage reverts (e.g. pool doesn't exist) should NOT call the
    expensive expected-amount math — we'd just waste an RPC round-trip."""
    from web3.exceptions import ContractLogicError

    spec = _MockSpec()
    _install_mock(monkeypatch, tmp_path, spec)

    monkeypatch.setattr(
        "wallet.cli.lp.prepare_mint",
        lambda *a, **kw: (_ for _ in ()).throw(ContractLogicError("execution reverted: PoolNotInitialized")),
    )

    called = {"hit": False}

    def must_not_be_called(*a, **kw):
        called["hit"] = True
        raise AssertionError("expected-amount helper must not run for non-slippage reverts")

    monkeypatch.setattr("wallet.cli.lp.compute_mint_expected_amounts", must_not_be_called)

    r = CliRunner().invoke(app, [
        "--json", "lp", "mint", "USDC", "WETH",
        "--fee", "500", "--tick-lower", "187500", "--tick-upper", "187650",
        "--amount-a", "10", "--amount-b", "0.0016",
    ])
    assert r.exit_code == 3
    assert called["hit"] is False
    env = json.loads(r.output.strip().splitlines()[-1])
    assert env["error"] == "simulation_reverted"
    assert "PoolNotInitialized" in env["reason"]
    assert env.get("data") is None or "lp_amount0_expected_wei" not in env["data"]

"""Tier 1.2 — unlimited approve warning surfaces in both JSON envelope and rich preview."""

from __future__ import annotations


from wallet.cli._common import _build_data, _warnings_for
from wallet.core.config import ChainConfig
from wallet.core.tokens import MAX_UINT256
from wallet.core.tx import PreparedTx


SEPOLIA = ChainConfig(
    name="sepolia",
    chain_id=11155111,
    rpc_url="http://x",
    explorer_api_url="http://x",
    explorer_tx_url="http://x/{tx}",
    native_symbol="ETH",
)


def _approve_pt(amount: int) -> PreparedTx:
    return PreparedTx(
        tx={"from": "0x" + "ff" * 20, "to": "0x" + "11" * 20,
            "gas": 50000, "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**9,
            "chainId": 11155111, "type": 2},
        estimated_fee_wei=50000 * 10**9,
        description={
            "kind": "USDC approve",
            "from": "0x" + "ff" * 20,
            "spender": "0x" + "22" * 20,
            "token_address": "0x" + "33" * 20,
            "amount_wei": amount,
            "amount_unit": "USDC",
            "amount_decimals": 6,
        },
    )


def test_unlimited_approve_emits_warning():
    pt = _approve_pt(MAX_UINT256)
    warnings = _warnings_for(pt)
    assert len(warnings) == 1
    assert warnings[0]["code"] == "unlimited_approve"
    assert warnings[0]["severity"] == "high"
    assert "drain" in warnings[0]["message"].lower()


def test_finite_approve_emits_no_warning():
    pt = _approve_pt(10**6)
    assert _warnings_for(pt) == []


def test_non_approve_with_max_uint_value_emits_no_warning():
    # Unrelated tx that happens to have a huge value field
    pt = PreparedTx(
        tx={}, estimated_fee_wei=0,
        description={
            "kind": "native transfer",
            "from": "0x" + "ff" * 20,
            "to": "0x" + "11" * 20,
            "amount_wei": MAX_UINT256,
            "amount_unit": "ETH",
            "amount_decimals": 18,
        },
    )
    assert _warnings_for(pt) == []


def test_build_data_includes_warnings_field_when_present():
    pt = _approve_pt(MAX_UINT256)
    d = _build_data(pt, SEPOLIA, phase="preview")
    assert "warnings" in d
    assert d["warnings"][0]["code"] == "unlimited_approve"


def test_build_data_omits_warnings_field_when_clean():
    pt = _approve_pt(10**6)
    d = _build_data(pt, SEPOLIA, phase="preview")
    assert "warnings" not in d

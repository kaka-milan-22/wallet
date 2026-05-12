"""Aave V3 view layer — mocked contract calls."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wallet.core.config import ChainConfig
from wallet.protocols.aave import (
    AAVE_BASE_DECIMALS,
    AaveReserve,
    base_to_usd,
    get_account_summary,
    get_all_rates,
    get_all_reserves,
    get_user_positions,
    ray_to_pct,
)


CHAIN = ChainConfig(
    name="sepolia", chain_id=11155111,
    rpc_url="http://invalid", explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}", native_symbol="ETH",
    protocols={
        "aave_v3": {
            "pool": "0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951",
            "data_provider": "0x3e9708d80f7B3e43118013075F7e95CE3AB31F31",
        },
    },
)

USDC_RESERVE = ("USDC", "0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8")
WETH_RESERVE = ("WETH", "0xC558DBdd856501FCd9aaF1E62eae57A9F0629a3c")
USER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def _fn(return_value):
    """Build a mock chained `.functions.foo(args).call()` returning a fixed value."""
    fn = MagicMock()
    fn.return_value.call.return_value = return_value
    return fn


# --- ray_to_pct / base_to_usd ------------------------------------------------


def test_ray_to_pct_zero():
    assert ray_to_pct(0) == "0.00"


def test_ray_to_pct_5_percent():
    # 5% = 0.05 in ray = 5 * 10^25
    assert ray_to_pct(5 * 10**25) == "5.00"


def test_ray_to_pct_complex():
    # 4.25% = 4.25 * 10^25
    assert ray_to_pct(425 * 10**23) == "4.25"


def test_base_to_usd():
    # 1.23 USD = 123_000_000 in base (1e8)
    assert base_to_usd(123_000_000) == "1.23"
    assert base_to_usd(0) == "0.00"


# --- get_all_reserves --------------------------------------------------------


def test_get_all_reserves_returns_dataclasses():
    dp = MagicMock()
    dp.functions.getAllReservesTokens = _fn([USDC_RESERVE, WETH_RESERVE])
    # Decimals via getReserveConfigurationData (first return is decimals)
    def cfg_fn(asset):
        if asset.lower() == USDC_RESERVE[1].lower():
            return MagicMock(call=lambda: [6] + [0] * 9)
        return MagicMock(call=lambda: [18] + [0] * 9)
    dp.functions.getReserveConfigurationData.side_effect = cfg_fn

    w3 = MagicMock()
    w3.eth.contract.return_value = dp

    reserves = get_all_reserves(w3, CHAIN)
    by_sym = {r.symbol: r for r in reserves}
    assert by_sym["USDC"].decimals == 6
    assert by_sym["WETH"].decimals == 18
    assert by_sym["USDC"].asset_address == USDC_RESERVE[1]


# --- get_account_summary -----------------------------------------------------


def test_account_summary_with_debt_computes_hf():
    pool = MagicMock()
    pool.functions.getUserAccountData = _fn([
        1_000_00000000,  # totalCollateralBase = $1000 (in base = USD*1e8)
        500_00000000,    # totalDebtBase = $500
        300_00000000,    # availableBorrowsBase = $300
        8000,            # liquidationThreshold = 80%
        7500,            # ltv = 75%
        16 * 10**17,     # healthFactor = 1.6 (in 1e18)
    ])
    w3 = MagicMock()
    w3.eth.contract.return_value = pool

    s = get_account_summary(w3, CHAIN, USER)
    assert s.total_collateral_base_wei == 1_000_00000000
    assert s.total_debt_base_wei == 500_00000000
    assert s.ltv_bps == 7500
    assert s.liquidation_threshold_bps == 8000
    assert abs(s.health_factor - 1.6) < 1e-9


def test_account_summary_no_debt_returns_none_hf():
    pool = MagicMock()
    pool.functions.getUserAccountData = _fn([0, 0, 0, 0, 0, 2**256 - 1])
    w3 = MagicMock()
    w3.eth.contract.return_value = pool

    s = get_account_summary(w3, CHAIN, USER)
    assert s.health_factor is None
    assert s.total_debt_base_wei == 0


def test_account_summary_huge_hf_clamped_to_none():
    """When debt is non-zero but HF is astronomical (e.g. 1e30), still treat as None."""
    pool = MagicMock()
    pool.functions.getUserAccountData = _fn([
        1_000_00000000, 1, 0, 0, 0, 2**200,  # debt=1 wei, HF practically infinite
    ])
    w3 = MagicMock()
    w3.eth.contract.return_value = pool

    s = get_account_summary(w3, CHAIN, USER)
    assert s.health_factor is None  # clamped via the 2**128 sentinel


# --- get_user_positions ------------------------------------------------------


def test_user_positions_filters_zero_balances():
    reserves = [
        AaveReserve(symbol="USDC", asset_address=USDC_RESERVE[1], decimals=6),
        AaveReserve(symbol="WETH", asset_address=WETH_RESERVE[1], decimals=18),
    ]

    dp = MagicMock()
    # getUserReserveData returns:
    #   [currentATokenBalance, currentStableDebt, currentVariableDebt, ...]
    def user_data_fn(asset, user):
        if asset.lower() == USDC_RESERVE[1].lower():
            return MagicMock(call=lambda: [100_000_000, 0, 0, 0, 0, 0, 0, 0, True])
        # WETH: both zero (will be filtered out)
        return MagicMock(call=lambda: [0, 0, 0, 0, 0, 0, 0, 0, False])
    dp.functions.getUserReserveData.side_effect = user_data_fn

    w3 = MagicMock()
    w3.eth.contract.return_value = dp

    positions = get_user_positions(w3, CHAIN, USER, reserves=reserves)
    assert len(positions) == 1
    assert positions[0].reserve.symbol == "USDC"
    assert positions[0].supplied_wei == 100_000_000
    assert positions[0].variable_debt_wei == 0


def test_user_positions_captures_borrow():
    reserves = [AaveReserve(symbol="USDC", asset_address=USDC_RESERVE[1], decimals=6)]

    dp = MagicMock()
    dp.functions.getUserReserveData = MagicMock(side_effect=lambda asset, user: MagicMock(
        call=lambda: [0, 0, 50_000_000, 0, 0, 0, 0, 0, False]  # only variable debt
    ))

    w3 = MagicMock()
    w3.eth.contract.return_value = dp

    positions = get_user_positions(w3, CHAIN, USER, reserves=reserves)
    assert len(positions) == 1
    assert positions[0].supplied_wei == 0
    assert positions[0].variable_debt_wei == 50_000_000


# --- get_all_rates -----------------------------------------------------------


def test_all_rates_parses_ray_indices():
    reserves = [AaveReserve(symbol="USDC", asset_address=USDC_RESERVE[1], decimals=6)]

    dp = MagicMock()
    # getReserveData returns 12 fields; we read index 5 (liquidityRate)
    # and index 6 (variableBorrowRate).
    def rd_fn(asset):
        return MagicMock(call=lambda: [
            0, 0, 0, 0, 0,
            5 * 10**25,  # supply APR = 5%
            8 * 10**25,  # variable borrow APR = 8%
            0, 0, 0, 0, 0,
        ])
    dp.functions.getReserveData.side_effect = rd_fn

    w3 = MagicMock()
    w3.eth.contract.return_value = dp

    rates = get_all_rates(w3, CHAIN, reserves=reserves)
    assert len(rates) == 1
    assert rates[0].supply_apr_ray == 5 * 10**25
    assert rates[0].variable_borrow_apr_ray == 8 * 10**25
    assert ray_to_pct(rates[0].supply_apr_ray) == "5.00"
    assert ray_to_pct(rates[0].variable_borrow_apr_ray) == "8.00"


def test_all_rates_empty_reserves_returns_empty():
    w3 = MagicMock()
    assert get_all_rates(w3, CHAIN, reserves=[]) == []

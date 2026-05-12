"""Aave V3 view layer (PR3 — read-only).

All reads go through Aave's `Pool.getUserAccountData` (summary including HF)
and `AaveProtocolDataProvider.{getAllReservesTokens, getReserveData,
getUserReserveData, getReserveConfigurationData}` (per-asset detail).

Per-reserve fan-out uses a ThreadPoolExecutor — Aave Sepolia advertises
~10 reserves, so sequential RPC would be a perceptible delay.

PR4 will add write helpers (`prepare_supply` / `prepare_withdraw` →
PreparedTx); PR5 will add borrow / repay + the `policy.min_health_factor`
gate. Both will reuse the data structures defined here.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from web3 import Web3

from wallet.core.config import ChainConfig, get_protocol_address

# `getUserAccountData` returns a base-currency that's USD-scaled with 8
# decimals on Aave V3 (changed from ETH-scaled in V2). The function returns
# raw integers; we expose `_base` fields and a human-readable USD string.
AAVE_BASE_DECIMALS = 8

# Aave rates are stored in "ray" — 1e27 base — and represent the annual
# compound rate. We convert to percentage by dividing by 1e25.
RAY = 10**27


@dataclass(frozen=True)
class AaveReserve:
    symbol: str
    asset_address: str  # the underlying ERC-20 (Aave mock on testnets)
    decimals: int


@dataclass(frozen=True)
class AaveReserveRates:
    reserve: AaveReserve
    supply_apr_ray: int  # liquidityRate from getReserveData
    variable_borrow_apr_ray: int  # variableBorrowRate from getReserveData


@dataclass(frozen=True)
class AaveUserPosition:
    reserve: AaveReserve
    supplied_wei: int  # currentATokenBalance
    variable_debt_wei: int  # currentVariableDebt (stable debt deprecated in V3)


@dataclass(frozen=True)
class AaveAccountSummary:
    total_collateral_base_wei: int  # in base currency (USD * 1e8 on V3)
    total_debt_base_wei: int
    available_borrows_base_wei: int
    ltv_bps: int  # max LTV in basis points (e.g. 7500 = 75%)
    liquidation_threshold_bps: int
    health_factor: float | None  # None when no debt (raw HF == max uint256)


# --- ABIs (minimal subsets) --------------------------------------------------


AAVE_POOL_ABI = [
    {
        "name": "getUserAccountData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
    },
    {
        "name": "supply",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "onBehalfOf", "type": "address"},
            {"name": "referralCode", "type": "uint16"},
        ],
        "outputs": [],
    },
    {
        "name": "withdraw",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "to", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


WITHDRAW_MAX_AMOUNT = 2**256 - 1  # Aave convention: type(uint256).max = "withdraw all"


AAVE_DATA_PROVIDER_ABI = [
    {
        "name": "getAllReservesTokens",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{
            "name": "tokens",
            "type": "tuple[]",
            "components": [
                {"name": "symbol", "type": "string"},
                {"name": "tokenAddress", "type": "address"},
            ],
        }],
    },
    {
        "name": "getReserveConfigurationData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "decimals", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "liquidationThreshold", "type": "uint256"},
            {"name": "liquidationBonus", "type": "uint256"},
            {"name": "reserveFactor", "type": "uint256"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
            {"name": "borrowingEnabled", "type": "bool"},
            {"name": "stableBorrowRateEnabled", "type": "bool"},
            {"name": "isActive", "type": "bool"},
            {"name": "isFrozen", "type": "bool"},
        ],
    },
    {
        "name": "getReserveData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [
            {"name": "unbacked", "type": "uint256"},
            {"name": "accruedToTreasuryScaled", "type": "uint256"},
            {"name": "totalAToken", "type": "uint256"},
            {"name": "totalStableDebt", "type": "uint256"},
            {"name": "totalVariableDebt", "type": "uint256"},
            {"name": "liquidityRate", "type": "uint256"},
            {"name": "variableBorrowRate", "type": "uint256"},
            {"name": "stableBorrowRate", "type": "uint256"},
            {"name": "averageStableBorrowRate", "type": "uint256"},
            {"name": "liquidityIndex", "type": "uint256"},
            {"name": "variableBorrowIndex", "type": "uint256"},
            {"name": "lastUpdateTimestamp", "type": "uint40"},
        ],
    },
    {
        "name": "getUserReserveData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "user", "type": "address"},
        ],
        "outputs": [
            {"name": "currentATokenBalance", "type": "uint256"},
            {"name": "currentStableDebt", "type": "uint256"},
            {"name": "currentVariableDebt", "type": "uint256"},
            {"name": "principalStableDebt", "type": "uint256"},
            {"name": "scaledVariableDebt", "type": "uint256"},
            {"name": "stableBorrowRate", "type": "uint256"},
            {"name": "liquidityRate", "type": "uint256"},
            {"name": "stableRateLastUpdated", "type": "uint40"},
            {"name": "usageAsCollateralEnabled", "type": "bool"},
        ],
    },
]


# --- helpers -----------------------------------------------------------------


def _pool(w3, chain: ChainConfig):
    return w3.eth.contract(
        address=Web3.to_checksum_address(get_protocol_address(chain, "aave_v3", "pool")),
        abi=AAVE_POOL_ABI,
    )


def _data_provider(w3, chain: ChainConfig):
    return w3.eth.contract(
        address=Web3.to_checksum_address(
            get_protocol_address(chain, "aave_v3", "data_provider")
        ),
        abi=AAVE_DATA_PROVIDER_ABI,
    )


def get_all_reserves(w3, chain: ChainConfig) -> list[AaveReserve]:
    """List every reserve Aave V3 currently supports on this chain."""
    dp = _data_provider(w3, chain)
    raw = dp.functions.getAllReservesTokens().call()  # list of (symbol, address)

    def fetch_config(asset_address: str) -> int:
        cfg = dp.functions.getReserveConfigurationData(
            Web3.to_checksum_address(asset_address)
        ).call()
        return int(cfg[0])  # decimals

    with ThreadPoolExecutor(max_workers=min(10, max(1, len(raw)))) as pool:
        decimals_list = list(pool.map(lambda r: fetch_config(r[1]), raw))

    return [
        AaveReserve(symbol=sym, asset_address=Web3.to_checksum_address(addr), decimals=dec)
        for (sym, addr), dec in zip(raw, decimals_list)
    ]


def get_account_summary(w3, chain: ChainConfig, user: str) -> AaveAccountSummary:
    """Read user-wide totals + health factor."""
    pool = _pool(w3, chain)
    result = pool.functions.getUserAccountData(Web3.to_checksum_address(user)).call()
    total_coll, total_debt, avail, liq_thresh, ltv, hf_raw = result

    # HF returns 2^256-1 when there's no debt (no liquidation risk)
    if total_debt == 0 or hf_raw >= 2**128:
        hf: float | None = None
    else:
        hf = hf_raw / 10**18

    return AaveAccountSummary(
        total_collateral_base_wei=int(total_coll),
        total_debt_base_wei=int(total_debt),
        available_borrows_base_wei=int(avail),
        ltv_bps=int(ltv),
        liquidation_threshold_bps=int(liq_thresh),
        health_factor=hf,
    )


def get_user_positions(
    w3, chain: ChainConfig, user: str, reserves: list[AaveReserve] | None = None
) -> list[AaveUserPosition]:
    """Fan out `getUserReserveData` across all reserves, return only non-zero entries."""
    if reserves is None:
        reserves = get_all_reserves(w3, chain)
    if not reserves:
        return []

    dp = _data_provider(w3, chain)
    user_cs = Web3.to_checksum_address(user)

    def fetch(reserve: AaveReserve):
        result = dp.functions.getUserReserveData(reserve.asset_address, user_cs).call()
        a_token = int(result[0])
        var_debt = int(result[2])
        return reserve, a_token, var_debt

    with ThreadPoolExecutor(max_workers=min(10, len(reserves))) as pool:
        rows = list(pool.map(fetch, reserves))

    positions: list[AaveUserPosition] = []
    for reserve, a_token, var_debt in rows:
        if a_token > 0 or var_debt > 0:
            positions.append(AaveUserPosition(
                reserve=reserve, supplied_wei=a_token, variable_debt_wei=var_debt,
            ))
    return positions


def get_all_rates(
    w3, chain: ChainConfig, reserves: list[AaveReserve] | None = None
) -> list[AaveReserveRates]:
    """Fan out `getReserveData` to read per-reserve APRs in ray."""
    if reserves is None:
        reserves = get_all_reserves(w3, chain)
    if not reserves:
        return []

    dp = _data_provider(w3, chain)

    def fetch(reserve: AaveReserve):
        result = dp.functions.getReserveData(reserve.asset_address).call()
        return AaveReserveRates(
            reserve=reserve,
            supply_apr_ray=int(result[5]),         # liquidityRate
            variable_borrow_apr_ray=int(result[6]),  # variableBorrowRate
        )

    with ThreadPoolExecutor(max_workers=min(10, len(reserves))) as pool:
        return list(pool.map(fetch, reserves))


# --- formatting helpers (used by both CLI and tests) -------------------------


def ray_to_pct(rate_ray: int) -> str:
    """Convert an Aave ray-encoded rate to a percentage string with 2 decimals."""
    if rate_ray == 0:
        return "0.00"
    # rate / 1e27 is the annual rate as a fraction; *100 → percent.
    pct = rate_ray / 10**25
    return f"{pct:.2f}"


def base_to_usd(base_wei: int) -> str:
    """Convert Aave's base-currency wei (USD × 1e8) to a human USD string."""
    if base_wei == 0:
        return "0.00"
    return f"{base_wei / 10**AAVE_BASE_DECIMALS:.2f}"


# --- reserve resolution ------------------------------------------------------


def resolve_aave_reserve(w3, chain: ChainConfig, query: str) -> AaveReserve:
    """Resolve a symbol ('USDC') or 0x address to an `AaveReserve`.

    Aave registers its own (often mock) tokens on testnets, distinct from the
    chain's builtin Circle USDC / canonical WETH. So we look up against
    Aave's actual reserve list, not chain.builtin_tokens.
    """
    reserves = get_all_reserves(w3, chain)
    q = query.strip()

    if q.startswith("0x"):
        for r in reserves:
            if r.asset_address.lower() == q.lower():
                return r
        raise ValueError(f"no Aave V3 reserve at address {q}")

    qu = q.upper()
    for r in reserves:
        if r.symbol.upper() == qu:
            return r
    raise ValueError(
        f"no Aave V3 reserve with symbol {q!r}. "
        f"Available: {', '.join(r.symbol for r in reserves)}"
    )


# --- write helpers: prepare_supply / prepare_withdraw -----------------------


# Imported lazily to avoid a hard dep when only view helpers are used
def _prepare_common_imports():
    from wallet.core.tokens import allowance
    from wallet.core.tx import PreparedTx, _common_fields, _simulate
    from wallet.protocols.swap import InsufficientAllowance
    return PreparedTx, _common_fields, _simulate, allowance, InsufficientAllowance


def prepare_supply(
    w3,
    chain: ChainConfig,
    sender: str,
    reserve: AaveReserve,
    amount_wei: int,
):
    """Build an unsigned `Pool.supply(asset, amount, onBehalfOf=sender, 0)` tx.

    Raises `InsufficientAllowance` (from `protocols.swap`) if the sender hasn't
    approved the Aave Pool for at least `amount_wei` of the asset. The
    `wallet swap` allowance-error envelope reuse means agents already know
    how to recover.
    """
    PreparedTx, _common_fields, _simulate, allowance, InsufficientAllowance = (
        _prepare_common_imports()
    )

    sender_cs = Web3.to_checksum_address(sender)
    pool_addr = Web3.to_checksum_address(
        get_protocol_address(chain, "aave_v3", "pool")
    )

    current = allowance(w3, reserve.asset_address, sender_cs, pool_addr)
    if current < amount_wei:
        raise InsufficientAllowance(
            token_symbol=reserve.symbol,
            token_address=reserve.asset_address,
            spender=pool_addr,
            current_wei=current,
            required_wei=amount_wei,
        )

    summary = get_account_summary(w3, chain, sender_cs)

    pool = w3.eth.contract(address=pool_addr, abi=AAVE_POOL_ABI)
    base = _common_fields(w3, chain, sender)
    tx = pool.functions.supply(
        Web3.to_checksum_address(reserve.asset_address),
        amount_wei,
        sender_cs,
        0,  # referralCode — Aave deprecated this; pass 0
    ).build_transaction(base)
    _simulate(w3, tx)
    fee_wei = tx["maxFeePerGas"] * tx["gas"]

    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee_wei,
        description={
            "kind": "aave supply",
            "from": tx["from"],
            "to": pool_addr,
            "amount_wei": amount_wei,
            "amount_unit": reserve.symbol,
            "amount_decimals": reserve.decimals,
            "aave_action": "supply",
            "aave_asset_address": reserve.asset_address,
            "aave_pool": pool_addr,
            "aave_current_hf": str(summary.health_factor) if summary.health_factor is not None else "inf",
        },
    )


def prepare_withdraw(
    w3,
    chain: ChainConfig,
    sender: str,
    reserve: AaveReserve,
    amount_wei: int,
):
    """Build an unsigned `Pool.withdraw(asset, amount, to=sender)` tx.

    Pass `amount_wei = WITHDRAW_MAX_AMOUNT` to withdraw the entire aToken
    balance (Aave convention). Aave reverts when the post-withdraw HF would
    drop below 1.0, which `_simulate` surfaces as a clean
    `simulation_reverted` error so the agent doesn't need to predict it.
    """
    PreparedTx, _common_fields, _simulate, _, _ = _prepare_common_imports()

    sender_cs = Web3.to_checksum_address(sender)
    pool_addr = Web3.to_checksum_address(
        get_protocol_address(chain, "aave_v3", "pool")
    )

    summary = get_account_summary(w3, chain, sender_cs)

    pool = w3.eth.contract(address=pool_addr, abi=AAVE_POOL_ABI)
    base = _common_fields(w3, chain, sender)
    tx = pool.functions.withdraw(
        Web3.to_checksum_address(reserve.asset_address),
        amount_wei,
        sender_cs,
    ).build_transaction(base)
    _simulate(w3, tx)
    fee_wei = tx["maxFeePerGas"] * tx["gas"]

    is_max = amount_wei == WITHDRAW_MAX_AMOUNT
    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee_wei,
        description={
            "kind": "aave withdraw",
            "from": tx["from"],
            "to": pool_addr,
            "amount_wei": amount_wei,
            "amount_unit": reserve.symbol,
            "amount_decimals": reserve.decimals,
            "aave_action": "withdraw",
            "aave_asset_address": reserve.asset_address,
            "aave_pool": pool_addr,
            "aave_current_hf": str(summary.health_factor) if summary.health_factor is not None else "inf",
            "aave_withdraw_max": is_max,
        },
    )

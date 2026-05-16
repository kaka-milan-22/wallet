"""Transaction pipeline: build → simulate → estimate → broadcast.

All sending commands (send / approve / revoke) go through this module so the
safety checks (revert detection, EIP-1559 fee estimation, gas limit estimation)
are uniform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from web3 import Web3
from web3.exceptions import ContractLogicError

from wallet.core.config import ChainConfig
from wallet.core.tokens import ERC20_ABI

__all__ = [
    "MIN_PRIORITY_GWEI",
    "PreparedTx",
    "broadcast",
    "finalize_tx",
    "prepare_erc20_approve",
    "prepare_erc20_transfer",
    "prepare_native_transfer",
]

MIN_PRIORITY_GWEI = 1  # floor below which we round up; Sepolia public RPC sometimes returns 0


@dataclass
class PreparedTx:
    tx: dict[str, Any]
    estimated_fee_wei: int
    description: dict[str, Any] = field(default_factory=dict)


def _fees(w3: Web3) -> tuple[int, int]:
    """Return (max_priority_fee_per_gas, max_fee_per_gas) in wei.

    Uses RPC `eth_maxPriorityFeePerGas` for priority and 2x current base fee as
    headroom. Floors priority at 1 gwei to avoid stuck txs on quiet testnets.
    """
    try:
        priority = int(w3.eth.max_priority_fee)
    except Exception:
        priority = 0
    floor = Web3.to_wei(MIN_PRIORITY_GWEI, "gwei")
    if priority < floor:
        priority = floor

    base = w3.eth.get_block("latest")["baseFeePerGas"]
    max_fee = base * 2 + priority
    return priority, max_fee


def _simulate(w3: Web3, tx: dict[str, Any]) -> None:
    """Run `eth_call` and surface revert reason as a clear error."""
    try:
        w3.eth.call(tx)
    except ContractLogicError as e:
        raise RuntimeError(f"simulation reverted: {e}") from e
    except ValueError as e:
        # Some RPC backends raise ValueError for revert
        raise RuntimeError(f"simulation failed: {e}") from e


def _common_fields(w3: Web3, chain: ChainConfig, sender: str) -> dict[str, Any]:
    # Note: nonce is intentionally NOT set here. We defer it to signing time
    # (see confirm_and_broadcast) so that the gap between dry-run and broadcast,
    # or between policy prompt and confirmation, can't ship a stale nonce
    # because another tx from the same account landed in between.
    priority, max_fee = _fees(w3)
    return {
        "from": Web3.to_checksum_address(sender),
        "chainId": chain.chain_id,
        "type": 2,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority,
    }


def _strip_nonce(tx: dict[str, Any]) -> dict[str, Any]:
    """`Contract.build_transaction(base)` auto-fills nonce when it isn't in
    `base`. We want it absent until confirm_and_broadcast refreshes it."""
    tx.pop("nonce", None)
    return tx


def finalize_tx(w3: Web3, tx: dict[str, Any]) -> int:
    """Finish a half-built tx and return the estimated fee in wei.

    Steps in order: estimate gas if not already set (handles both the manual
    `tx = {...}` path and the `contract.functions.x(...).build_transaction()`
    path, the latter of which pre-fills `gas`); pre-simulate via eth_call to
    surface reverts before signing; strip nonce so confirm_and_broadcast can
    re-fetch it at sign-time; return `maxFeePerGas * gas` so the caller's
    `PreparedTx.estimated_fee_wei` matches what will actually leave the wallet.

    Centralises the four lines every prepare_* used to repeat verbatim. The
    only good reason to bypass this helper is a tx that legitimately doesn't
    want pre-simulation (none today).
    """
    if "gas" not in tx:
        tx["gas"] = w3.eth.estimate_gas(tx)
    _simulate(w3, tx)
    _strip_nonce(tx)
    return int(tx["maxFeePerGas"]) * int(tx["gas"])


def prepare_native_transfer(
    w3: Web3,
    chain: ChainConfig,
    sender: str,
    recipient: str,
    amount_wei: int,
) -> PreparedTx:
    tx = _common_fields(w3, chain, sender)
    tx["to"] = Web3.to_checksum_address(recipient)
    tx["value"] = amount_wei
    fee = finalize_tx(w3, tx)
    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee,
        description={
            "kind": "native transfer",
            "from": tx["from"],
            "to": tx["to"],
            "amount_wei": amount_wei,
            "amount_unit": chain.native_symbol,
            "amount_decimals": 18,
        },
    )


def prepare_erc20_transfer(
    w3: Web3,
    chain: ChainConfig,
    sender: str,
    token_address: str,
    recipient: str,
    amount: int,
    token_symbol: str,
    token_decimals: int,
) -> PreparedTx:
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_address), abi=ERC20_ABI
    )
    base = _common_fields(w3, chain, sender)
    tx = contract.functions.transfer(
        Web3.to_checksum_address(recipient), amount
    ).build_transaction(base)
    fee = finalize_tx(w3, tx)
    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee,
        description={
            "kind": f"{token_symbol} transfer",
            "from": tx["from"],
            "to": Web3.to_checksum_address(recipient),
            "token_address": Web3.to_checksum_address(token_address),
            "amount_wei": amount,
            "amount_unit": token_symbol,
            "amount_decimals": token_decimals,
        },
    )


def prepare_erc20_approve(
    w3: Web3,
    chain: ChainConfig,
    sender: str,
    token_address: str,
    spender: str,
    amount: int,
    token_symbol: str,
    token_decimals: int,
) -> PreparedTx:
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_address), abi=ERC20_ABI
    )
    base = _common_fields(w3, chain, sender)
    tx = contract.functions.approve(
        Web3.to_checksum_address(spender), amount
    ).build_transaction(base)
    fee = finalize_tx(w3, tx)
    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee,
        description={
            "kind": f"{token_symbol} approve",
            "from": tx["from"],
            "spender": Web3.to_checksum_address(spender),
            "token_address": Web3.to_checksum_address(token_address),
            "amount_wei": amount,
            "amount_unit": token_symbol,
            "amount_decimals": token_decimals,
        },
    )


def broadcast(w3: Web3, raw_tx: bytes) -> str:
    """Submit a signed raw transaction; return tx hash hex string."""
    h = w3.eth.send_raw_transaction(raw_tx)
    return h.hex() if not h.hex().startswith("0x") else h.hex()

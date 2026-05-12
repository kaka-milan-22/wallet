"""High-level swap: pluggable RouteProvider → PreparedTx.

Reuses `core/tx.py:_common_fields / _simulate` so swap txs flow through the
same gas / nonce / simulation pipeline as transfers and approvals. The
returned PreparedTx feeds straight into `cli/_common.py:confirm_and_broadcast`
which handles policy / idempotency / audit / signing.

Allowance is **pre-checked** here: if the sender hasn't approved the router
yet, we raise `InsufficientAllowance` before constructing the swap tx. The
CLI surface converts that to an `insufficient_allowance` error envelope so
agents know to invoke `wallet approve set` first.
"""

from __future__ import annotations

from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.core.tokens import TokenInfo, allowance
from wallet.core.tx import PreparedTx, _common_fields, _simulate
from wallet.protocols.routes.base import RouteProvider


class InsufficientAllowance(RuntimeError):
    """Raised when the sender's ERC-20 allowance to the router is below amount_in.

    Carries the data needed by the CLI to suggest the corrective `wallet approve set`
    command.
    """

    def __init__(
        self,
        *,
        token_symbol: str,
        token_address: str,
        spender: str,
        current_wei: int,
        required_wei: int,
    ):
        self.token_symbol = token_symbol
        self.token_address = token_address
        self.spender = spender
        self.current_wei = current_wei
        self.required_wei = required_wei
        super().__init__(
            f"allowance for {token_symbol} to {spender} is "
            f"{current_wei} < {required_wei} required"
        )


def prepare_swap(
    w3: Web3,
    chain: ChainConfig,
    sender: str,
    token_in: TokenInfo,
    token_out: TokenInfo,
    amount_in_wei: int,
    slippage_bps: int,
    provider: RouteProvider,
) -> PreparedTx:
    quote = provider.quote(
        w3=w3, chain=chain, sender=sender,
        token_in=token_in, token_out=token_out,
        amount_in_wei=amount_in_wei, slippage_bps=slippage_bps,
    )

    # Allowance pre-check (skip for native ETH input — value=msg.value, no transferFrom)
    is_native_in = token_in.symbol == chain.native_symbol
    if not is_native_in:
        current = allowance(w3, token_in.address, sender, quote.spender)
        if current < amount_in_wei:
            raise InsufficientAllowance(
                token_symbol=token_in.symbol,
                token_address=token_in.address,
                spender=quote.spender,
                current_wei=current,
                required_wei=amount_in_wei,
            )

    base = _common_fields(w3, chain, sender)
    tx = {
        **base,
        "to": Web3.to_checksum_address(quote.to),
        "value": quote.value,
        "data": quote.data,
    }
    tx["gas"] = w3.eth.estimate_gas(tx)
    _simulate(w3, tx)
    fee_wei = tx["maxFeePerGas"] * tx["gas"]

    return PreparedTx(
        tx=tx,
        estimated_fee_wei=fee_wei,
        description={
            "kind": "swap",
            "from": tx["from"],
            "to": quote.to,  # router; used by audit + policy.contract_allowlist
            "amount_wei": amount_in_wei,
            "amount_unit": token_in.symbol,
            "amount_decimals": token_in.decimals,
            "swap_token_in_address": quote.token_in_address,
            "swap_token_out_address": quote.token_out_address,
            "swap_token_out_symbol": token_out.symbol,
            "swap_token_out_decimals": token_out.decimals,
            "swap_amount_out_expected_wei": quote.amount_out_expected_wei,
            "swap_amount_out_min_wei": quote.amount_out_min_wei,
            "swap_slippage_bps": slippage_bps,
            "swap_route": quote.route_description,
            "swap_provider": quote.route_provider,
        },
    )

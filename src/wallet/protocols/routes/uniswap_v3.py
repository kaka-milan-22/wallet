"""Direct Uniswap V3 single-hop swap route.

Uses QuoterV2 (off-chain quote via eth_call) to pick the fee tier with the
deepest liquidity, then encodes a SwapRouter02 `exactInputSingle` call.
Multi-hop (V3 path encoding through intermediate tokens) is deferred.
"""

from __future__ import annotations

from web3 import Web3

from wallet.core.config import ChainConfig, get_protocol_address
from wallet.core.slippage import apply_slippage_floor
from wallet.core.tokens import TokenInfo
from wallet.protocols.routes.base import NoRouteError, Quote, RouteProvider

# Fee tiers used by Uniswap V3 pools (basis points × 100).
# 100 = 0.01% (stable / stable), 500 = 0.05%, 3000 = 0.3% (default), 10000 = 1% (exotic)
FEE_TIERS = [100, 500, 3000, 10000]


QUOTER_V2_ABI = [
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            },
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    },
]


SWAP_ROUTER_V2_ABI = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            },
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    # `multicall(bytes[])` chains exactInputSingle + unwrapWETH9 in a single
    # tx so a user who asked for native ETH out actually receives ETH (not
    # WETH). Without this the router leaves a WETH balance in the user's
    # account and they need a separate unwrap step — surprising semantics.
    {
        "name": "multicall",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "data", "type": "bytes[]"}],
        "outputs": [{"name": "results", "type": "bytes[]"}],
    },
    # `unwrapWETH9(amountMinimum, recipient)` withdraws all WETH held by the
    # router and forwards it as native ETH to `recipient`. Used as the
    # second call of the multicall when token_out is native.
    {
        "name": "unwrapWETH9",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "amountMinimum", "type": "uint256"},
            {"name": "recipient", "type": "address"},
        ],
        "outputs": [],
    },
]


# SwapRouter02 treats this sentinel as "recipient is the router itself" so
# the swap output stays inside the router; the next call in the multicall
# (unwrapWETH9) then forwards it to the real user. Constant defined in
# Periphery contracts as MSG_SENDER (1) / ADDRESS_THIS (2). We use
# ADDRESS_THIS = 2 because msg.sender is the user's EOA (no help).
_ROUTER_AS_RECIPIENT = "0x0000000000000000000000000000000000000002"


class UniswapV3DirectRoute(RouteProvider):
    name = "uniswap_v3"

    def quote(
        self,
        w3: Web3,
        chain: ChainConfig,
        sender: str,
        token_in: TokenInfo,
        token_out: TokenInfo,
        amount_in_wei: int,
        slippage_bps: int,
    ) -> Quote:
        quoter_addr = Web3.to_checksum_address(
            get_protocol_address(chain, "uniswap_v3", "quoter_v2")
        )
        router_addr = Web3.to_checksum_address(
            get_protocol_address(chain, "uniswap_v3", "swap_router_v2")
        )

        # Native ETH input: calldata uses WETH address (router wraps via msg.value).
        # Native ETH output: calldata still references WETH; the router writes WETH
        # into itself (recipient = ADDRESS_THIS sentinel), and a second multicall
        # leg `unwrapWETH9` converts to native ETH and forwards to the user.
        # Route on token_*.is_native (set only by the CLI's native-symbol branch);
        # symbol() is attacker-controlled for 0x… tokens. See security_review.md Vuln 1.
        is_native_in = token_in.is_native
        is_native_out = token_out.is_native
        effective_in_address = (
            Web3.to_checksum_address(chain.builtin_tokens["WETH"])
            if is_native_in
            else Web3.to_checksum_address(token_in.address)
        )
        effective_out_address = (
            Web3.to_checksum_address(chain.builtin_tokens["WETH"])
            if is_native_out
            else Web3.to_checksum_address(token_out.address)
        )

        quoter = w3.eth.contract(address=quoter_addr, abi=QUOTER_V2_ABI)

        # Try each fee tier; keep the one with deepest quoted output.
        best_fee: int | None = None
        best_out: int = 0
        for fee in FEE_TIERS:
            try:
                result = quoter.functions.quoteExactInputSingle(
                    (effective_in_address, effective_out_address, amount_in_wei, fee, 0)
                ).call()
            except Exception:
                # Pool doesn't exist or no liquidity at this tier; try the next one.
                continue
            amount_out = int(result[0])
            if amount_out > best_out:
                best_fee = fee
                best_out = amount_out

        if best_fee is None or best_out == 0:
            raise NoRouteError(
                f"no liquidity for {token_in.symbol} → {token_out.symbol} "
                f"on any Uniswap V3 fee tier (tried {FEE_TIERS})"
            )

        amount_out_min = apply_slippage_floor(best_out, slippage_bps)

        router = w3.eth.contract(address=router_addr, abi=SWAP_ROUTER_V2_ABI)
        # When output is native ETH, the swap recipient must be the router
        # itself (ADDRESS_THIS sentinel) so the WETH stays in the router for
        # the unwrap leg; the unwrap then forwards real ETH to the user.
        swap_recipient = (
            _ROUTER_AS_RECIPIENT if is_native_out
            else Web3.to_checksum_address(sender)
        )
        swap_calldata = router.encode_abi(
            "exactInputSingle",
            args=[(
                effective_in_address,
                effective_out_address,
                best_fee,
                swap_recipient,
                amount_in_wei,
                amount_out_min,
                0,  # sqrtPriceLimitX96 — no price limit
            )],
        )
        if is_native_out:
            unwrap_calldata = router.encode_abi(
                "unwrapWETH9",
                args=[amount_out_min, Web3.to_checksum_address(sender)],
            )
            data = router.encode_abi(
                "multicall",
                args=[[bytes.fromhex(swap_calldata[2:]), bytes.fromhex(unwrap_calldata[2:])]],
            )
        else:
            data = swap_calldata

        return Quote(
            route_provider=self.name,
            route_description=f"{token_in.symbol} > {best_fee}bps > {token_out.symbol}",
            to=router_addr,
            data=data,
            value=amount_in_wei if is_native_in else 0,
            token_in_address=effective_in_address,
            token_out_address=effective_out_address,
            token_in_symbol=token_in.symbol,
            token_out_symbol=token_out.symbol,
            token_in_decimals=token_in.decimals,
            token_out_decimals=token_out.decimals,
            amount_in_wei=amount_in_wei,
            amount_out_expected_wei=best_out,
            amount_out_min_wei=amount_out_min,
            spender=router_addr,
        )

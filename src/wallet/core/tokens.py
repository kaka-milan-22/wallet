from __future__ import annotations

from dataclasses import dataclass

from web3 import Web3
from web3.contract import Contract

from wallet.core.config import ChainConfig
from wallet.storage.state import WalletState

ERC20_ABI: list[dict] = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

MAX_UINT256 = 2**256 - 1


@dataclass(frozen=True)
class TokenInfo:
    symbol: str
    address: str  # checksummed
    decimals: int
    # True only when constructed by the CLI's native-symbol branch (e.g. "ETH").
    # Routes / swap orchestration MUST use this flag — never symbol comparison —
    # to decide msg.value vs ERC-20 transferFrom, since `symbol` for a 0x… token
    # comes from the contract itself and is attacker-controlled.
    is_native: bool = False


def erc20(w3: Web3, address: str) -> Contract:
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)


# Per-(chain_id, address) cache. ERC-20 symbol/decimals are immutable on-chain,
# so safe to memoize for the process lifetime. Bounds the long tail of token
# lookups in `portfolio` / `swap` / `resolve_token` to one round-trip per token
# instead of one per command.
_TOKEN_INFO_CACHE: dict[tuple[int, str], TokenInfo] = {}


def fetch_token_info(w3: Web3, address: str) -> TokenInfo:
    cs = Web3.to_checksum_address(address)
    try:
        chain_id = int(w3.eth.chain_id)
    except Exception:
        chain_id = 0  # untrusted mock / disconnected w3 — skip cache
    key = (chain_id, cs.lower())
    if chain_id and key in _TOKEN_INFO_CACHE:
        return _TOKEN_INFO_CACHE[key]

    c = erc20(w3, address)
    info = TokenInfo(
        symbol=c.functions.symbol().call(),
        address=cs,
        decimals=c.functions.decimals().call(),
    )
    if chain_id:
        _TOKEN_INFO_CACHE[key] = info
    return info


def clear_token_info_cache() -> None:
    """Test hook — wipe the process-wide cache so each case starts fresh."""
    _TOKEN_INFO_CACHE.clear()


def balance_of(w3: Web3, token_address: str, owner: str) -> int:
    return (
        erc20(w3, token_address)
        .functions.balanceOf(Web3.to_checksum_address(owner))
        .call()
    )


def allowance(w3: Web3, token_address: str, owner: str, spender: str) -> int:
    return (
        erc20(w3, token_address)
        .functions.allowance(
            Web3.to_checksum_address(owner),
            Web3.to_checksum_address(spender),
        )
        .call()
    )


def resolve_token(
    w3: Web3,
    chain: ChainConfig,
    state: WalletState,
    query: str,
) -> TokenInfo:
    """Resolve a token reference to TokenInfo.

    Order of resolution:
      1. 0x-prefixed contract address — fetch live
      2. user-added symbol in state.tokens (cached decimals)
      3. chain-builtin symbol (USDC/WETH/...)
    """
    q = query.strip()
    if q.startswith("0x"):
        return fetch_token_info(w3, q)

    qu = q.upper()
    for t in state.tokens:
        if t.symbol.upper() == qu and t.chain == chain.name:
            return TokenInfo(symbol=t.symbol, address=t.address, decimals=t.decimals)

    for sym, addr in chain.builtin_tokens.items():
        if sym.upper() == qu:
            return fetch_token_info(w3, addr)

    raise ValueError(f"unknown token '{query}' on chain {chain.name}")

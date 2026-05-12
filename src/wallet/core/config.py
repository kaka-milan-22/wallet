from __future__ import annotations

import json
import os
from pathlib import Path

from platformdirs import user_data_dir
from pydantic import BaseModel, Field


class ChainConfig(BaseModel):
    name: str
    chain_id: int
    rpc_url: str
    explorer_api_url: str
    explorer_tx_url: str  # template with {tx}
    native_symbol: str
    builtin_tokens: dict[str, str] = Field(default_factory=dict)
    protocols: dict[str, dict[str, str]] = Field(default_factory=dict)
    """Per-protocol contract addresses, keyed by protocol name then field.

    Example: protocols["uniswap_v3"]["swap_router_v2"] = "0x...".
    Lookup via `get_protocol_address(chain, "uniswap_v3", "swap_router_v2")`."""


_BUILTIN_PRESETS: dict[str, dict] = {
    "sepolia": {
        "name": "sepolia",
        "chain_id": 11155111,
        "rpc_url_env": "WALLET_SEPOLIA_RPC",
        "rpc_url_default": "https://ethereum-sepolia.publicnode.com",
        "explorer_api_url": "https://api.etherscan.io/v2/api",
        "explorer_tx_url": "https://sepolia.etherscan.io/tx/{tx}",
        "native_symbol": "ETH",
        "builtin_tokens": {
            # Circle official Sepolia USDC
            "USDC": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
            # Canonical WETH9 on Sepolia
            "WETH": "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",
        },
        "protocols": {
            "uniswap_v3": {
                "swap_router_v2": "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E",
                "quoter_v2": "0xEd1f6473345F45b75F8179591dd5bA1888cf2FB3",
                "factory": "0x0227628f3F023bb0B980b67D528571c95c6DaC1c",
            },
            "aave_v3": {
                "pool": "0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951",
                "data_provider": "0x3e9708d80f7B3e43118013075F7e95CE3AB31F31",
                "addresses_provider": "0x012bAC54348C0E635dCAc9D5FB99f06F24136C9A",
                "faucet": "0xC959483DBa39aa9E78757139af0e9a2EDEb3f42D",
                "oracle": "0x2da88497588bf89281816106C7259e31AF45a663",
            },
        },
    },
}


def chains_config_path() -> Path:
    return Path(user_data_dir("wallet", appauthor=False)) / "chains.json"


def get_chain(name: str = "sepolia") -> ChainConfig:
    user_overrides: dict = {}
    p = chains_config_path()
    if p.exists():
        user_overrides = json.loads(p.read_text())

    if name in user_overrides:
        return ChainConfig(**user_overrides[name])

    if name not in _BUILTIN_PRESETS:
        raise ValueError(f"Unknown chain: {name}. Built-in: {list(_BUILTIN_PRESETS)}")

    preset = _BUILTIN_PRESETS[name]
    rpc_url = os.getenv(preset["rpc_url_env"], preset["rpc_url_default"])
    return ChainConfig(
        name=preset["name"],
        chain_id=preset["chain_id"],
        rpc_url=rpc_url,
        explorer_api_url=preset["explorer_api_url"],
        explorer_tx_url=preset["explorer_tx_url"],
        native_symbol=preset["native_symbol"],
        builtin_tokens=preset["builtin_tokens"],
        protocols=preset.get("protocols", {}),
    )


def list_chains() -> list[str]:
    return list(_BUILTIN_PRESETS.keys())


def get_protocol_address(chain: ChainConfig, protocol: str, field: str) -> str:
    """Look up a protocol contract address from the chain config.

    Raises ValueError if the protocol or field isn't configured for this chain.
    """
    p = chain.protocols.get(protocol, {})
    addr = p.get(field)
    if not addr:
        raise ValueError(
            f"Chain {chain.name!r} has no `{protocol}.{field}` configured. "
            f"Add it to chains.json or extend the built-in preset."
        )
    return addr

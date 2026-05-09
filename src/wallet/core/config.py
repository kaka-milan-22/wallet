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
    )


def list_chains() -> list[str]:
    return list(_BUILTIN_PRESETS.keys())

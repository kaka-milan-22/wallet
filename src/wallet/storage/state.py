from __future__ import annotations

from pathlib import Path

from wallet.core.config import atomic_write_text, data_root
from pydantic import BaseModel, Field


class AccountEntry(BaseModel):
    name: str
    address: str  # EIP-55 checksummed
    derivation_path: str
    vault_key: str  # agent-vault key holding the mnemonic


class WatchEntry(BaseModel):
    address: str
    label: str | None = None


class TokenEntry(BaseModel):
    symbol: str
    address: str
    decimals: int
    chain: str


class WalletState(BaseModel):
    default_account: str | None = None
    default_chain: str = "sepolia"
    accounts: list[AccountEntry] = Field(default_factory=list)
    book: dict[str, str] = Field(default_factory=dict)
    watch: list[WatchEntry] = Field(default_factory=list)
    tokens: list[TokenEntry] = Field(default_factory=list)

    def find_account(self, name: str) -> AccountEntry | None:
        return next((a for a in self.accounts if a.name == name), None)

    def get_default_account(self) -> AccountEntry | None:
        if self.default_account is None:
            return self.accounts[0] if self.accounts else None
        return self.find_account(self.default_account)


def state_dir() -> Path:
    p = data_root()
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path() -> Path:
    return state_dir() / "state.json"


def load_state() -> WalletState:
    p = state_path()
    if not p.exists():
        return WalletState()
    return WalletState.model_validate_json(p.read_text())


def save_state(state: WalletState) -> None:
    atomic_write_text(state_path(), state.model_dump_json(indent=2))

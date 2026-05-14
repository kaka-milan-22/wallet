"""BIP-39 mnemonic + BIP-44 derivation for Ethereum.

Uses `eth-account`'s built-in HD wallet support. The mnemonic feature is gated
behind `enable_unaudited_hdwallet_features()` — we enable it once on import.
For the curve / derivation logic itself, eth-account delegates to mature C
libraries (libsecp256k1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eth_account import Account
from eth_account.hdaccount import ValidationError as _MnemonicValidationError

__all__ = ["DerivedAccount", "default_path", "derive", "generate_mnemonic", "is_valid_mnemonic"]

# Required since eth-account 0.5.0; the feature is stable but not formally audited.
Account.enable_unaudited_hdwallet_features()


def default_path(index: int = 0) -> str:
    """Standard Ethereum BIP-44 path (matches MetaMask, Ledger Live, Trezor)."""
    return f"m/44'/60'/0'/0/{index}"


@dataclass(frozen=True)
class DerivedAccount:
    address: str  # EIP-55 checksummed
    path: str
    # `repr=False` is load-bearing security: the default dataclass __repr__ would
    # print the full private key bytes any time this object hits a traceback,
    # debugger watch, logging call, or accidental print(). The whole "secret
    # never enters LLM context" invariant relies on these 32 bytes staying off
    # stdout/stderr — making them invisible to repr is the right default.
    private_key: bytes = field(repr=False)


def generate_mnemonic() -> str:
    """Generate a fresh 12-word English mnemonic with 128 bits of entropy."""
    _, mnemonic = Account.create_with_mnemonic(num_words=12)
    return mnemonic


def derive(mnemonic: str, index: int = 0, path: str | None = None) -> DerivedAccount:
    """Derive an Ethereum account from `mnemonic` at the given index or full path.

    Either `index` (using the default Ethereum BIP-44 path) or an explicit `path`
    must be provided; if both are given, `path` wins.
    """
    p = path or default_path(index)
    acct = Account.from_mnemonic(mnemonic, account_path=p)
    return DerivedAccount(address=acct.address, path=p, private_key=bytes(acct.key))


def is_valid_mnemonic(mnemonic: str) -> bool:
    """Return True iff `mnemonic` parses as a valid BIP-39 phrase."""
    try:
        Account.from_mnemonic(mnemonic, account_path=default_path(0))
        return True
    except (_MnemonicValidationError, ValueError):
        # eth-account 0.13 raises ValidationError for bad words / wrong length;
        # ValueError covers edge cases (empty string, non-string input). We
        # deliberately don't swallow other exceptions so real bugs surface.
        return False

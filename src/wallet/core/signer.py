"""Sign transactions for accounts whose mnemonic is stored in alice (AnB).

The mnemonic is fetched, the private key is derived, the tx is signed, and the
mnemonic / key references are dropped before this function returns. The signed
raw bytes are the only thing that escapes.
"""

from __future__ import annotations

from typing import Any

from eth_account import Account

from wallet.core import hd
from wallet.storage import vault
from wallet.storage.state import AccountEntry


def sign_transaction(
    account: AccountEntry,
    tx: dict[str, Any],
    *,
    reason: str | None = None,
) -> bytes:
    """Sign `tx` with the account's mnemonic-derived private key.

    `reason` is forwarded to alice via `--reason` so Bob's JSON audit captures
    why the mnemonic was decrypted. Callers (each CLI signing command) should
    pass a short verb-and-target string, e.g. "send 0.01 ETH to @alice via
    wallet (request_id=...)" — this lets `bob.log` correlate with wallet's
    own audit log without sharing any state.
    """
    if not vault.has(account.vault_key):
        raise RuntimeError(
            f"vault key '{account.vault_key}' is empty — run "
            f"`alice set {account.vault_key}` first"
        )

    mnemonic = vault.reveal(account.vault_key, reason=reason)
    try:
        derived = hd.derive(mnemonic, path=account.derivation_path)
    finally:
        del mnemonic

    if derived.address.lower() != account.address.lower():
        raise RuntimeError(
            f"address mismatch: vault mnemonic derives {derived.address} "
            f"but account '{account.name}' is registered as {account.address}. "
            f"The wrong mnemonic may be stored under '{account.vault_key}'."
        )

    signed = Account.sign_transaction(tx, derived.private_key)
    # eth-account 0.13 renamed `rawTransaction` → `raw_transaction`
    return getattr(signed, "raw_transaction", None) or signed.rawTransaction  # type: ignore[attr-defined]

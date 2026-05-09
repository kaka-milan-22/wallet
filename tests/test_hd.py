"""HD derivation correctness against well-known fixed mnemonics.

Hardhat ships with a fixed test mnemonic and the addresses derived from it are
documented and reproduced across most Ethereum tooling — they are de-facto
test vectors.
"""

from wallet.core.hd import default_path, derive, generate_mnemonic, is_valid_mnemonic

HARDHAT_MNEMONIC = "test test test test test test test test test test test junk"

# Addresses for m/44'/60'/0'/0/{0..2} — reproduced by ethers.js, foundry, hardhat.
HARDHAT_ADDRESSES = [
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
    "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
]


def test_default_path():
    assert default_path(0) == "m/44'/60'/0'/0/0"
    assert default_path(7) == "m/44'/60'/0'/0/7"


def test_hardhat_vectors_index_path_equivalence():
    for i, want in enumerate(HARDHAT_ADDRESSES):
        a = derive(HARDHAT_MNEMONIC, index=i)
        assert a.address == want, f"index={i}: got {a.address}, want {want}"
        assert a.path == default_path(i)
        assert isinstance(a.private_key, bytes)
        assert len(a.private_key) == 32

        b = derive(HARDHAT_MNEMONIC, path=default_path(i))
        assert b.address == want
        assert b.private_key == a.private_key


def test_explicit_path_overrides_index():
    a = derive(HARDHAT_MNEMONIC, index=99, path=default_path(0))
    assert a.address == HARDHAT_ADDRESSES[0]


def test_is_valid_mnemonic():
    assert is_valid_mnemonic(HARDHAT_MNEMONIC)
    assert is_valid_mnemonic(
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )
    assert not is_valid_mnemonic("not a real mnemonic phrase at all")
    assert not is_valid_mnemonic("")


def test_generate_mnemonic_is_valid_and_distinct():
    a = generate_mnemonic()
    b = generate_mnemonic()
    assert len(a.split()) == 12
    assert len(b.split()) == 12
    assert a != b  # cryptographically vanishing collision probability
    assert is_valid_mnemonic(a)
    assert is_valid_mnemonic(b)

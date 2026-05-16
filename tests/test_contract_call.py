"""contract_call: signature parsing, arg coercion, calldata build, prepare."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from web3 import Web3

from wallet.core.config import ChainConfig
from wallet.protocols.contract_call import (
    ArgCoercionError,
    SignatureParseError,
    build_calldata,
    coerce_arg,
    parse_function_signature,
    prepare_contract_call,
)


SEPOLIA = ChainConfig(
    name="sepolia", chain_id=11155111,
    rpc_url="http://invalid", explorer_api_url="http://invalid",
    explorer_tx_url="http://invalid/{tx}", native_symbol="ETH",
    builtin_tokens={},
)
SENDER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
TARGET = "0x" + "33" * 20


# --- parse_function_signature ------------------------------------------------


def test_parse_simple_signature():
    name, types = parse_function_signature("transfer(address,uint256)")
    assert name == "transfer"
    assert types == ["address", "uint256"]


def test_parse_zero_arg_signature():
    name, types = parse_function_signature("name()")
    assert name == "name"
    assert types == []


def test_parse_array_type():
    name, types = parse_function_signature("claim(uint256[])")
    assert name == "claim"
    assert types == ["uint256[]"]


def test_parse_whitespace_tolerant():
    name, types = parse_function_signature("  approve (   address , uint256 )  ")
    assert name == "approve"
    assert types == ["address", "uint256"]


def test_parse_tuple_explicitly_rejected():
    """Tuples deferred to typed prepare_* until we need them."""
    with pytest.raises(SignatureParseError, match="tuple"):
        parse_function_signature("mint((address,address,uint24))")


def test_parse_unknown_type_rejected():
    with pytest.raises(SignatureParseError, match="unsupported"):
        parse_function_signature("foo(notatype)")


def test_parse_malformed_signature_rejected():
    with pytest.raises(SignatureParseError):
        parse_function_signature("not_a_function")


# --- coerce_arg --------------------------------------------------------------


def test_coerce_address_validates_format():
    out = coerce_arg("0x" + "ab" * 20, "address")
    assert out == Web3.to_checksum_address("0x" + "ab" * 20)


def test_coerce_address_rejects_short():
    with pytest.raises(ArgCoercionError, match="20-byte"):
        coerce_arg("0xabcd", "address")


def test_coerce_uint_decimal_and_hex():
    assert coerce_arg("100", "uint256") == 100
    assert coerce_arg("0x64", "uint256") == 100


def test_coerce_uint_rejects_garbage():
    with pytest.raises(ArgCoercionError, match="not a valid integer"):
        coerce_arg("not-a-number", "uint256")


def test_coerce_bool_accepts_common_forms():
    assert coerce_arg("true", "bool") is True
    assert coerce_arg("FALSE", "bool") is False
    assert coerce_arg("1", "bool") is True
    assert coerce_arg("0", "bool") is False


def test_coerce_bool_rejects_other():
    with pytest.raises(ArgCoercionError, match="bool must be"):
        coerce_arg("maybe", "bool")


def test_coerce_bytes_fixed_length_validated():
    # bytes32 is exactly 32 bytes
    good = "0x" + "ab" * 32
    out = coerce_arg(good, "bytes32")
    assert out == bytes.fromhex("ab" * 32)

    bad = "0x" + "ab" * 16  # 16 bytes, not 32
    with pytest.raises(ArgCoercionError, match="expected 32 bytes"):
        coerce_arg(bad, "bytes32")


def test_coerce_dynamic_bytes_any_length():
    out = coerce_arg("0xdeadbeef", "bytes")
    assert out == b"\xde\xad\xbe\xef"


def test_coerce_array_takes_json():
    out = coerce_arg("[1, 2, 3]", "uint256[]")
    assert out == [1, 2, 3]


def test_coerce_array_of_addresses():
    a1 = "0x" + "11" * 20
    a2 = "0x" + "22" * 20
    out = coerce_arg(f'["{a1}", "{a2}"]', "address[]")
    assert out == [Web3.to_checksum_address(a1), Web3.to_checksum_address(a2)]


def test_coerce_array_rejects_non_json():
    with pytest.raises(ArgCoercionError, match="JSON"):
        coerce_arg("1,2,3", "uint256[]")


# --- build_calldata ----------------------------------------------------------


def test_build_calldata_matches_known_selector():
    """ERC-20 transfer selector is fixed at a9059cbb — a property of the
    function signature, not our encoder. If we ever produce something else
    here, every wallet on earth disagrees with us."""
    cd, name, types, typed = build_calldata(
        "transfer(address,uint256)",
        ["0x" + "11" * 20, "100"],
    )
    assert name == "transfer"
    assert types == ["address", "uint256"]
    assert typed == [Web3.to_checksum_address("0x" + "11" * 20), 100]
    assert cd.startswith("0xa9059cbb")


def test_build_calldata_arity_mismatch_raises():
    with pytest.raises(ArgCoercionError, match="takes 2 args, got 1"):
        build_calldata("transfer(address,uint256)", ["0x" + "11" * 20])


def test_build_calldata_zero_arg_function():
    cd, name, types, typed = build_calldata("name()", [])
    assert name == "name"
    assert types == []
    assert typed == []
    # No-arg selector is just the 4-byte hash with no encoded args.
    assert len(cd) == 2 + 8  # "0x" + 4 bytes hex


# --- prepare_contract_call --------------------------------------------------


def _w3_mock():
    w3 = MagicMock()
    w3.eth.max_priority_fee = Web3.to_wei(2, "gwei")
    w3.eth.get_block.return_value = {"baseFeePerGas": Web3.to_wei(10, "gwei")}
    w3.eth.estimate_gas.return_value = 50_000
    w3.eth.call.return_value = b""
    return w3


def test_prepare_contract_call_builds_tx_and_description():
    w3 = _w3_mock()
    pt = prepare_contract_call(
        w3=w3, chain=SEPOLIA, sender=SENDER,
        to=TARGET, fn_sig="transfer(address,uint256)",
        args=["0x" + "11" * 20, "100"],
        value_wei=0,
    )

    assert pt.tx["to"] == Web3.to_checksum_address(TARGET)
    assert pt.tx["value"] == 0
    assert pt.tx["data"].startswith("0xa9059cbb")
    assert pt.tx["chainId"] == 11155111
    assert "nonce" not in pt.tx  # deferred to sign-time

    d = pt.description
    assert d["kind"] == "contract call transfer"
    assert d["cc_function_signature"] == "transfer(address,uint256)"
    assert d["cc_function_name"] == "transfer"
    assert d["cc_calldata"] == pt.tx["data"]
    assert d["amount_wei"] == 0
    assert d["amount_unit"] == "ETH"  # value goes through the per-tx ETH cap
    # Args are stringified for JSON safety (int → str, bytes → 0xhex)
    assert d["cc_args"] == [
        {"type": "address", "value": Web3.to_checksum_address("0x" + "11" * 20)},
        {"type": "uint256", "value": "100"},
    ]


def test_prepare_contract_call_with_native_value():
    w3 = _w3_mock()
    pt = prepare_contract_call(
        w3=w3, chain=SEPOLIA, sender=SENDER,
        to=TARGET, fn_sig="deposit()", args=[],
        value_wei=10**16,  # 0.01 ETH
    )
    assert pt.tx["value"] == 10**16
    # amount_wei mirrors value_wei so existing max_per_tx{ETH} catches it
    assert pt.description["amount_wei"] == 10**16
    assert pt.description["amount_unit"] == "ETH"


def test_prepare_contract_call_surfaces_simulation_revert():
    from web3.exceptions import ContractLogicError

    w3 = _w3_mock()
    w3.eth.call.side_effect = ContractLogicError("execution reverted: not owner")

    with pytest.raises(RuntimeError, match="simulation reverted"):
        prepare_contract_call(
            w3=w3, chain=SEPOLIA, sender=SENDER,
            to=TARGET, fn_sig="transferOwnership(address)",
            args=["0x" + "11" * 20],
            value_wei=0,
        )

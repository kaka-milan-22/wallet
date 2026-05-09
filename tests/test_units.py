"""Fixed-point amount formatting and parsing."""

import pytest

from wallet.core.rpc import format_units, parse_units


def test_format_units_zero_decimals():
    assert format_units(0, 0) == "0"
    assert format_units(1234, 0) == "1234"


def test_format_units_eth():
    assert format_units(0, 18) == "0"
    assert format_units(10**18, 18) == "1"
    assert format_units(15 * 10**17, 18) == "1.5"
    assert format_units(1, 18) == "0.000000000000000001"
    assert format_units(123456789012345678, 18) == "0.123456789012345678"


def test_format_units_usdc():
    assert format_units(1_000_000, 6) == "1"
    assert format_units(1_500_000, 6) == "1.5"
    assert format_units(1, 6) == "0.000001"


def test_parse_units_eth():
    assert parse_units("1", 18) == 10**18
    assert parse_units("1.5", 18) == 15 * 10**17
    assert parse_units("0.000000000000000001", 18) == 1
    assert parse_units("0", 18) == 0


def test_parse_units_usdc():
    assert parse_units("1", 6) == 1_000_000
    assert parse_units("0.5", 6) == 500_000
    assert parse_units("0.000001", 6) == 1


def test_parse_units_rejects_excess_precision():
    with pytest.raises(ValueError, match="fractional digits"):
        parse_units("1.0000001", 6)


def test_parse_units_rejects_invalid():
    with pytest.raises(ValueError):
        parse_units("not a number", 18)
    with pytest.raises(ValueError):
        parse_units("", 18)


def test_format_parse_roundtrip():
    samples = [0, 1, 12345, 10**18, 999_999_999_999_999_999_999]
    for raw in samples:
        s = format_units(raw, 18)
        assert parse_units(s, 18) == raw

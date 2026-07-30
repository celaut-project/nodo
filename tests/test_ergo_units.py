"""Unit tests for Decimal->nanoERG conversion and pure Ergo address validation (#186)."""
import pytest

from src.utils.ergo_units import (
    NANOERG_PER_ERG,
    erg_to_nanoerg,
    is_valid_ergo_address,
    nanoerg_to_erg_str,
)

# A real mainnet P2PK address (the default donation wallet shipped in config.example.yaml).
VALID_MAINNET = "9gGZp7HRAFxgGWSwvS4hCbxM2RpkYr6pHvwpU4GPrpvxY7Y2nQo"


@pytest.mark.parametrize("value,expected", [
    ("0", 0),
    ("1", NANOERG_PER_ERG),
    ("100", 100 * NANOERG_PER_ERG),
    ("0.5", 500_000_000),
    ("0.000000001", 1),
    (2, 2 * NANOERG_PER_ERG),
])
def test_erg_to_nanoerg_valid(value, expected):
    assert erg_to_nanoerg(value) == expected


@pytest.mark.parametrize("bad", ["-1", "abc", "", "0.0000000001", None, True, float("nan")])
def test_erg_to_nanoerg_rejects(bad):
    with pytest.raises(ValueError):
        erg_to_nanoerg(bad)


def test_nanoerg_to_erg_str_is_fixed_point():
    assert nanoerg_to_erg_str(100 * NANOERG_PER_ERG) == "100"
    assert nanoerg_to_erg_str(1) == "0.000000001"
    assert nanoerg_to_erg_str(1_500_000_000) == "1.5"
    assert nanoerg_to_erg_str(0) == "0"


def test_address_validation_accepts_real_mainnet_p2pk():
    assert is_valid_ergo_address(VALID_MAINNET)


def test_address_validation_rejects_tampered_and_garbage():
    assert not is_valid_ergo_address(VALID_MAINNET[:-1] + "X")  # bad checksum
    assert not is_valid_ergo_address("not-an-address")
    assert not is_valid_ergo_address("")
    assert not is_valid_ergo_address("0OIl")  # non-base58 characters
    assert not is_valid_ergo_address(VALID_MAINNET, network="testnet")  # wrong network

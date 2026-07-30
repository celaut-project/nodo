"""ErgoTree conversion utility tests (#186 phase 3.1) — pure, no JVM."""
import pytest

from src.utils.ergo_tree import (
    as_bytes,
    ergo_trees_equal,
    is_p2pk_proposition,
    p2pk_proposition_bytes_from_pk,
)

COMPRESSED_PK = bytes.fromhex("02" + "ab" * 32)  # 33-byte SEC-compressed point
P2PK = bytes.fromhex("0008cd") + COMPRESSED_PK


def test_p2pk_proposition_bytes_from_pk_roundtrip():
    prop = p2pk_proposition_bytes_from_pk(COMPRESSED_PK)
    assert prop == P2PK
    assert is_p2pk_proposition(prop)
    assert is_p2pk_proposition(prop.hex())


@pytest.mark.parametrize("bad_len", [32, 34, 0])
def test_p2pk_rejects_wrong_length(bad_len):
    with pytest.raises(ValueError):
        p2pk_proposition_bytes_from_pk(b"\x02" * bad_len)


def test_as_bytes_normalizes_hex_and_bytes():
    assert as_bytes("0008cd") == b"\x00\x08\xcd"
    assert as_bytes(b"\x00\x08\xcd") == b"\x00\x08\xcd"
    with pytest.raises(TypeError):
        as_bytes(123)


def test_ergo_trees_equal_is_canonical_byte_comparison():
    # Same value as bytes vs hex compares equal; different bytes do not.
    assert ergo_trees_equal(P2PK, P2PK.hex())
    assert ergo_trees_equal(P2PK.hex().upper(), P2PK)  # hex case-insensitive
    assert not ergo_trees_equal(P2PK, P2PK[:-1])


def test_is_p2pk_rejects_non_p2pk():
    assert not is_p2pk_proposition(b"\x10\x01\x02\x03")  # wrong prefix
    assert not is_p2pk_proposition(bytes.fromhex("0008cd") + b"\x02" * 32)  # short point

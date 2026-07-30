"""
Raw-ErgoTree conversion utilities.

The canonical value nodo exchanges with peers in a ``Contract`` (its ``script`` xattr)
and stores in a reputation box' ``R7`` is the raw **ErgoTree / propositionBytes** — never
an ErgoScript source string and never a base58 address. Readable addresses are derived
only at the AppKit/UI/API boundary.

Pure-Python helpers (no JVM):
    * ``p2pk_proposition_bytes_from_pk`` — build P2PK propositionBytes from a 33-byte
      compressed public key.
    * ``ergo_trees_equal`` — canonical byte comparison of two ErgoTrees.
    * ``as_bytes`` — normalize hex-or-bytes to bytes.

AppKit-boundary helpers (lazy JVM import, only when an address/contract object is needed):
    * ``proposition_bytes_from_address`` — address string -> raw propositionBytes.
    * ``address_from_proposition_bytes`` — raw propositionBytes -> AppKit ``Address``.
    * ``ergo_contract_from_proposition_bytes`` — raw propositionBytes -> ``ErgoContract``.
    * ``serialize_ergo_tree`` — compiled ErgoTree object -> raw bytes.
"""
from __future__ import annotations

from typing import Union

# P2PK propositionBytes are always `0008cd` + 33-byte compressed group element.
_P2PK_PREFIX = bytes.fromhex("0008cd")
_P2PK_LEN = len(_P2PK_PREFIX) + 33


def as_bytes(value: Union[bytes, bytearray, str]) -> bytes:
    """Normalize an ErgoTree given as raw bytes or a hex string to ``bytes``."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return bytes.fromhex(value.strip())
    raise TypeError(f"Unsupported ErgoTree representation: {type(value)!r}")


def ergo_trees_equal(a: Union[bytes, str], b: Union[bytes, str]) -> bool:
    """True when two ErgoTrees are byte-identical under one canonical representation."""
    return as_bytes(a) == as_bytes(b)


def p2pk_proposition_bytes_from_pk(pk_bytes: Union[bytes, str]) -> bytes:
    """
    Build the raw P2PK propositionBytes (`0008cd` + compressed pubkey) from a 33-byte
    SEC-compressed public key. This is the value a reputation box stores in R7 and the
    payment contract advertises as its ``script``.
    """
    pk = as_bytes(pk_bytes)
    if len(pk) != 33:
        raise ValueError(f"Expected a 33-byte compressed public key, got {len(pk)} bytes.")
    return _P2PK_PREFIX + pk


def is_p2pk_proposition(proposition_bytes: Union[bytes, str]) -> bool:
    """True when the bytes look like a P2PK ErgoTree (`0008cd` + 33-byte point)."""
    raw = as_bytes(proposition_bytes)
    return len(raw) == _P2PK_LEN and raw.startswith(_P2PK_PREFIX)


# --------------------------------------------------------------------------- #
# AppKit boundary — only reached when an actual address/contract object is
# required (building/validating a transaction, or rendering an address for UI).
# --------------------------------------------------------------------------- #
def _org_appkit():
    from src.utils.java_dependency import ensure_ergpy_jvm, require_java_module

    ensure_ergpy_jvm(feature="Ergo ErgoTree conversion")
    jpype = require_java_module("jpype", feature="Ergo ErgoTree conversion")
    return jpype, jpype.JPackage("org").ergoplatform.appkit


def serialize_ergo_tree(ergo_tree) -> bytes:
    """Serialize a compiled AppKit/sigmastate ErgoTree object to raw propositionBytes."""
    jpype, _ = _org_appkit()
    serializer = jpype.JPackage("sigmastate").serialization.ErgoTreeSerializer.DefaultSerializer()
    return bytes((b + 256) % 256 for b in serializer.serializeErgoTree(ergo_tree))


def proposition_bytes_from_address(address: str) -> bytes:
    """Raw propositionBytes (serialized ErgoTree) of a base58 Ergo address string."""
    _, org_appkit = _org_appkit()
    addr = org_appkit.Address.create(address)
    return serialize_ergo_tree(addr.getErgoAddress().script())


def address_from_proposition_bytes(proposition_bytes: Union[bytes, str], mainnet: bool = True):
    """
    Reconstruct an AppKit ``Address`` from raw propositionBytes — the inverse of
    :func:`proposition_bytes_from_address`. Used only where an API/UI needs a readable
    address; never for peer exchange.
    """
    jpype, org_appkit = _org_appkit()
    raw = as_bytes(proposition_bytes)
    serializer = jpype.JPackage("sigmastate").serialization.ErgoTreeSerializer.DefaultSerializer()
    ergo_tree = serializer.deserializeErgoTree(jpype.JArray(jpype.JByte)(list(raw)))
    network = org_appkit.NetworkType.MAINNET if mainnet else org_appkit.NetworkType.TESTNET
    return org_appkit.Address.fromErgoTree(ergo_tree, network)


def ergo_contract_from_proposition_bytes(proposition_bytes: Union[bytes, str]):
    """Build an AppKit ``ErgoContract`` from raw propositionBytes for output boxes."""
    return address_from_proposition_bytes(proposition_bytes).toErgoContract()

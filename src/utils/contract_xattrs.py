from __future__ import annotations

from typing import Optional

from protos import celaut_pb2
from src.utils import keyvalue

SCRIPT_KEY = "script"
ADDRESS_KEY = "address"
TOKEN_ID_KEY = "token_id"
# The kind of payment contract is not an xattr. It is ``Contract.protocol``, and its
# ``formal`` defines these keys (#671). See ``src/utils/payment_protocol.py``.
# Who published this proof, and that owner proving it is the peer announcing it. The
# ledger's own key, in the ledger's own encoding, plus its signature over the announcing
# peer's id (node_identity.attestation_payload) -- see `attest_proof_ownership`.
#
# Per proof rather than per peer, because ownership is a property of the proof: a peer
# holds as many as it likes (issue #281) and nothing says they share an owner. The
# alternative, one attestation per ledger on the Peer, cannot express two proofs on the
# same ledger under different wallets.
OWNER_PUBLIC_KEY_KEY = "owner_public_key"
OWNER_SIGNATURE_KEY = "owner_signature"


def set_xattr_text(contract: celaut_pb2.Contract, key: str, value: str) -> None:
    keyvalue.set_value(contract.xattrs, key, value.encode("utf-8"))


def get_xattr_text(contract: celaut_pb2.Contract, key: str) -> str:
    value = keyvalue.get(contract.xattrs, key, b"")
    if not value:
        return ""
    return value.decode("utf-8")


def set_script(contract: celaut_pb2.Contract, script: bytes) -> None:
    keyvalue.set_value(contract.xattrs, SCRIPT_KEY, script)


def get_script(contract: celaut_pb2.Contract) -> bytes:
    return keyvalue.get(contract.xattrs, SCRIPT_KEY, b"")


def set_address(contract: celaut_pb2.Contract, address: str) -> None:
    set_xattr_text(contract, ADDRESS_KEY, address)


def get_address(contract: celaut_pb2.Contract) -> str:
    return get_xattr_text(contract, ADDRESS_KEY)


def set_token_id(contract: celaut_pb2.Contract, token_id: str) -> None:
    set_xattr_text(contract, TOKEN_ID_KEY, token_id)


def get_token_id(contract: celaut_pb2.Contract) -> str:
    return get_xattr_text(contract, TOKEN_ID_KEY)


def set_owner_attestation(
    contract: celaut_pb2.Contract, public_key: str, signature: str
) -> None:
    """Record who owns this proof and their signature over the peer id claiming it."""
    set_xattr_text(contract, OWNER_PUBLIC_KEY_KEY, public_key)
    set_xattr_text(contract, OWNER_SIGNATURE_KEY, signature)


def get_owner_attestation(contract: celaut_pb2.Contract) -> tuple:
    """``(public key, signature)`` of the claimed owner; either may be empty."""
    return (
        get_xattr_text(contract, OWNER_PUBLIC_KEY_KEY),
        get_xattr_text(contract, OWNER_SIGNATURE_KEY),
    )


def first_non_empty(*values: Optional[str]) -> str:
    for value in values:
        if value:
            return value
    return ""

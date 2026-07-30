from __future__ import annotations

from typing import Optional

from protos import celaut_pb2

SCRIPT_KEY = "script"
ADDRESS_KEY = "address"
TOKEN_ID_KEY = "token_id"
REPUTATION_KEY_KEY = "reputation_key"
# Stable, wallet-independent contract-type identity used to match the same kind of
# payment contract across nodes (its sha3 is the contract_hash). Distinct from the
# per-instance ``script`` xattr, which holds the raw ErgoTree/propositionBytes of the
# specific wallet box and varies per node.
CONTRACT_TYPE_KEY = "contract_type"


def set_xattr_text(contract: celaut_pb2.Contract, key: str, value: str) -> None:
    contract.xattrs[key] = value.encode("utf-8")


def get_xattr_text(contract: celaut_pb2.Contract, key: str) -> str:
    value = contract.xattrs.get(key, b"")
    if not value:
        return ""
    return value.decode("utf-8")


def set_script(contract: celaut_pb2.Contract, script: bytes) -> None:
    contract.xattrs[SCRIPT_KEY] = script


def get_script(contract: celaut_pb2.Contract) -> bytes:
    return contract.xattrs.get(SCRIPT_KEY, b"")


def set_address(contract: celaut_pb2.Contract, address: str) -> None:
    set_xattr_text(contract, ADDRESS_KEY, address)


def get_address(contract: celaut_pb2.Contract) -> str:
    return get_xattr_text(contract, ADDRESS_KEY)


def set_token_id(contract: celaut_pb2.Contract, token_id: str) -> None:
    set_xattr_text(contract, TOKEN_ID_KEY, token_id)


def get_token_id(contract: celaut_pb2.Contract) -> str:
    return get_xattr_text(contract, TOKEN_ID_KEY)


def set_reputation_key(contract: celaut_pb2.Contract, reputation_key: str) -> None:
    set_xattr_text(contract, REPUTATION_KEY_KEY, reputation_key)


def get_reputation_key(contract: celaut_pb2.Contract) -> str:
    return get_xattr_text(contract, REPUTATION_KEY_KEY)


def set_contract_type(contract: celaut_pb2.Contract, contract_type: bytes) -> None:
    contract.xattrs[CONTRACT_TYPE_KEY] = contract_type


def get_contract_type(contract: celaut_pb2.Contract) -> bytes:
    return contract.xattrs.get(CONTRACT_TYPE_KEY, b"")


def contract_shape_bytes(contract: celaut_pb2.Contract) -> bytes:
    normalized = celaut_pb2.Contract()
    normalized.ledger.CopyFrom(contract.ledger)
    for key in sorted(contract.xattrs.keys()):
        normalized.xattrs[key] = contract.xattrs[key]
    return normalized.SerializeToString()


def first_non_empty(*values: Optional[str]) -> str:
    for value in values:
        if value:
            return value
    return ""

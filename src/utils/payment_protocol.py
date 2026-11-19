"""The identity of a payment protocol: the bytes of its ``formal``, and nothing else (#671).

A payment contract carries a ``Contract.Protocol`` (tags, prose, formal). The formal
defines the keys of ``xattrs`` and the way to pay. Two payment protocols are the same
only if their formal fields have the same bytes. ``tags`` help a reader. ``prose`` never
decides.

One kind of line is not part of the identity: a **payee parameter**. It is a value that
the payee selects and states for the payer (``payment.min_confirmations``, #626). Two
nodes that select different values still use the same protocol. ``protocol_id`` is the
formal without these lines. The hash of a contract (``contract_hash``) is the SHA3-256 of
this value.

This module imports nothing from the node, so the database and the contracts can use it.
"""
from hashlib import sha3_256
from typing import Optional

MIN_CONFIRMATIONS_KEY = "payment.min_confirmations"
TOO_EARLY_KEY = "payment.too_early"

# Keys of a payment protocol that the payee selects for itself and states for the payer.
PAYEE_PARAMETERS = frozenset({MIN_CONFIRMATIONS_KEY, TOO_EARLY_KEY})


def formal_lines(formal: bytes):
    return bytes(formal).split(b"\n")


def without_payee_parameters(formal: bytes) -> bytes:
    """``formal`` less its payee-parameter lines, every other byte as it was."""
    return b"\n".join(
        line for line in formal_lines(formal)
        if line.split(b"=", 1)[0].decode("utf-8", "replace") not in PAYEE_PARAMETERS
    )


def protocol_id(protocol) -> bytes:
    """What identifies ``protocol``: its formal without the payee parameters.

    Empty when the protocol is missing or has no formal. A node does not use such a
    contract.
    """
    formal = bytes(getattr(protocol, "formal", b"") or b"")
    return without_payee_parameters(formal) if formal else b""


def contract_protocol_id(contract) -> bytes:
    """``protocol_id`` of the protocol of a ``Contract``, or empty if it has none."""
    if not contract.HasField("protocol"):
        return b""
    return protocol_id(contract.protocol)


def protocol_hash(protocol) -> Optional[str]:
    """The ``contract_hash`` of ``protocol``, or None when it has no formal."""
    identity = protocol_id(protocol)
    return sha3_256(identity).hexdigest() if identity else None


def same_payment_protocol(a, b) -> bool:
    """Whether two payment protocols are the same: the same ``protocol_id``, not empty."""
    identity = protocol_id(a)
    return bool(identity) and identity == protocol_id(b)

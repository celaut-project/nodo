"""The signature of a balance statement (#570), with no database.

``balance_statement`` signs and reads statements on the wire. The digest and the check
are here, apart from it, so that a reader of signed evidence (``fault_proofs``, #652)
can check a statement of any node without the state of this node.

The signature covers a canonical text of every field, never ``SerializeToString()``:
protobuf serialization is not canonical (docs/EXECUTION_RECEIPTS.md). The celaut-gateway
layer announces the same encoding (``statement.digest`` in transport_stack).
"""
import hashlib

from protos import celaut_pb2
from src.database import ledger
from src.identity.node_identity import statement_payload, verify_peer_payload


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def movement_hash(movement: celaut_pb2.BalanceMovement) -> str:
    """The hash of a movement of a statement, from its fields."""
    return ledger.movement_hash(
        movement.prev_hash, movement.seq, movement.ts, movement.amount.n,
        movement.balance_after.n, movement.kind, movement.instance_ref, movement.detail,
    )


def statement_digest(statement: celaut_pb2.BalanceStatement) -> str:
    """Blake2b-256 hex of the canonical text of every field but the signature.

    ``service_id`` is in the text only when it is not empty (``|service_id:`` and its
    hex, after the movements). ``instance_ref`` is in the text only when it is not empty
    (``|instance_ref:`` and its hex, after ``service_id``). ``relayed`` is in the text
    only when it is true (``|relayed`` at the end). So the digest of a statement without
    them does not change (#631).
    """
    text = "|".join((
        statement.account_ref, str(statement.from_seq), str(statement.to_seq),
        str(statement.ts), str(statement.retention_days), str(statement.oldest_seq),
        str(statement.head_seq), statement.head_hash, _hex(statement.balance.n),
        "/".join(movement_hash(m) for m in statement.movements),
    ))
    if statement.service_id:
        text += "|service_id:" + _hex(statement.service_id)
    if statement.instance_ref:
        text += "|instance_ref:" + _hex(statement.instance_ref)
    if statement.relayed:
        text += "|relayed"
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def verify(statement: celaut_pb2.BalanceStatement, peer_id: str) -> bool:
    """Whether ``peer_id`` signed exactly this statement."""
    return verify_peer_payload(
        peer_id, statement_payload(peer_id, statement_digest(statement)), statement.signature,
    )

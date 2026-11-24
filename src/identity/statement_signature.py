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

    The parts are in the order of the numbers of the fields in ``celaut.proto``, joined by
    ``|``: ``ts``, ``relayed`` (1 or 0), ``account_ref``, ``hex(service_id)``,
    ``hex(instance_ref)``, ``from_seq``, ``to_seq``, the hash of each movement joined by
    ``/``, ``hex(balance.n)``, ``head_seq``, ``head_hash``, ``oldest_seq`` and
    ``retention_days``. A part with no value is empty.
    """
    text = "|".join((
        str(statement.ts), "1" if statement.relayed else "0", statement.account_ref,
        _hex(statement.service_id), _hex(statement.instance_ref),
        str(statement.from_seq), str(statement.to_seq),
        "/".join(movement_hash(m) for m in statement.movements),
        _hex(statement.balance.n), str(statement.head_seq), statement.head_hash,
        str(statement.oldest_seq), str(statement.retention_days),
    ))
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def verify(statement: celaut_pb2.BalanceStatement, peer_id: str) -> bool:
    """Whether ``peer_id`` signed exactly this statement."""
    return verify_peer_payload(
        peer_id, statement_payload(peer_id, statement_digest(statement)), statement.signature,
    )

"""GetBalanceStatement (#570): the movements of an account, signed by the node.

Two sides. ``answer`` is this node answering the owner of an account: it charges the
caller, reads the rows in the same transaction, and signs the result. ``fetch`` is
this node reading its own account on a peer, or a child that it delegated there, and
it refuses a statement that the peer did not sign.

A statement signs the head of the account and the hash of every movement it shows. The
hash of a movement commits to the movement and to the one before it
(src/database/ledger.py), so a peer that signed a statement cannot show other movements
for the same seqs afterwards. The statement never carries a credential: the account is
named by ``account_ref``, and the instances by ``instance_ref``.

The signature covers a canonical text of every field, never ``SerializeToString()``:
protobuf serialization is not canonical (docs/EXECUTION_RECEIPTS.md). The celaut-gateway
layer announces the same encoding (``statement.digest`` in transport_stack), so any
reader can check a statement without this code.
"""
import hashlib

from protos import celaut_pb2
from src.database import ledger
from src.database.sql_connection import SQLConnection
from src.identity.node_identity import (
    get_node_public_key_hex,
    sign_peer_payload,
    statement_payload,
    verify_peer_payload,
)
from src.utils.monetary import prices
from src.utils.utils import to_amount

sc = SQLConnection()


class StatementRefused(Exception):
    """The caller may not read that account."""


class StatementInvalid(Exception):
    """A peer answered with no statement, or with one that its key did not sign."""


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def movement_hash(movement: celaut_pb2.BalanceMovement) -> str:
    """The hash of a movement of a statement, from its fields."""
    return ledger.movement_hash(
        movement.prev_hash, movement.seq, movement.ts, movement.amount.n,
        movement.balance_after.n, movement.kind, movement.instance_ref, movement.detail,
    )


def statement_digest(statement: celaut_pb2.BalanceStatement) -> str:
    """Blake2b-256 hex of the canonical text of every field but the signature."""
    text = "|".join((
        statement.account_ref, str(statement.from_seq), str(statement.to_seq),
        str(statement.ts), str(statement.retention_days), str(statement.oldest_seq),
        str(statement.head_seq), statement.head_hash, _hex(statement.balance.n),
        "/".join(movement_hash(m) for m in statement.movements),
    ))
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def verify(statement: celaut_pb2.BalanceStatement, peer_id: str) -> bool:
    """Whether ``peer_id`` signed exactly this statement."""
    return verify_peer_payload(
        peer_id, statement_payload(peer_id, statement_digest(statement)), statement.signature,
    )


def statement_price_mu(rows: int) -> int:
    """What a statement with ``rows`` movements costs: a fixed part and a part per row."""
    p = prices()
    return p.statement_query_mu + p.statement_row_mu * int(rows)


class StatementUnavailable(Exception):
    """This node has no statement of that account: it is an instance that it delegated."""


def answer(caller: str, token: str = "", from_seq: int = 0, to_seq: int = 0,
           allow_debt: bool = False) -> celaut_pb2.BalanceStatement:
    """The signed statement that ``caller`` asked for, charged to ``caller``.

    ``caller`` is who ``require_caller`` identified: a client_id, or a local instance.
    ``token`` names the account: empty for the caller's own, or a child of the caller.
    Each statement holds the rows of one account and nothing else: a child's rows are
    in the child's statement, which its father asks for by its token.

    An instance that this node delegated to a peer has no statement: its rows would
    show the delegation, and for the service nobody else runs its instance.
    """
    account = token or caller
    if account != caller and sc.father_of(account) != caller:
        raise StatementRefused(f"{account} is not the account of the caller or of a child of it.")
    if sc.delegated_account_exists(account):
        raise StatementUnavailable("This node gives no statement of that instance.")

    read = sc.balance_statement(
        account, caller, from_seq=int(from_seq), to_seq=int(to_seq),
        max_rows=ledger.max_statement_rows(), price=statement_price_mu,
        allow_debt=allow_debt,
    )
    statement = celaut_pb2.BalanceStatement(
        account_ref=ledger.account_ref(account),
        from_seq=int(from_seq),
        to_seq=int(to_seq),
        movements=[
            celaut_pb2.BalanceMovement(
                seq=int(row["seq"]),
                ts=int(row["ts"]),
                amount=to_amount(row["amount_mu"]),
                balance_after=to_amount(row["balance_after_mu"]),
                kind=row["kind"],
                instance_ref=row["instance_ref"] or "",
                detail=row["detail"] or "",
                prev_hash=row["prev_hash"],
            )
            for row in read["movements"]
        ],
        balance=to_amount(read["balance_mu"]),
        head_seq=int(read["head_seq"]),
        head_hash=read["head_hash"],
        oldest_seq=int(read["oldest_seq"]),
        retention_days=ledger.retention_days(),
        ts=read["ts"],
    )
    public_key = get_node_public_key_hex()
    signature = public_key and sign_peer_payload(
        statement_payload(public_key, statement_digest(statement))
    )
    # With no identity key there is nothing to sign with. The statement still goes out,
    # and its reader refuses it, as it refuses any unsigned one.
    statement.signature = signature or ""
    return statement


def fetch(peer_id: str, client_id: str, token: str = "", from_seq: int = 0,
          to_seq: int = 0) -> celaut_pb2.BalanceStatement:
    """Read a statement from ``peer_id`` as ``client_id``, and check its signature.

    ``token`` is a child that this node delegated there, by the token of the peer.
    Raises ``StatementInvalid`` for no answer, an answer about another account or
    range, or a signature that does not verify. A transport error is raised as it is,
    with its status code.
    """
    from src.identity.grpc_transport import peer_channel
    from src.utils.bee_client import BeeClient

    statement = BeeClient.get_balance_statement(
        peer_channel(peer_id=peer_id), client_id=client_id, token=token,
        from_seq=from_seq, to_seq=to_seq,
    )
    if statement is None:
        raise StatementInvalid(f"{peer_id} answered no statement.")
    if statement.account_ref != ledger.account_ref(token or client_id) \
            or statement.from_seq != from_seq or statement.to_seq != to_seq:
        raise StatementInvalid(f"{peer_id} answered a statement that was not asked for.")
    if not verify(statement, peer_id):
        raise StatementInvalid(f"The statement from {peer_id} is not signed by it.")
    return statement

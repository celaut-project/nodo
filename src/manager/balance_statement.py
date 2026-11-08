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
import time
from typing import Optional

from protos import celaut_pb2
from src.database import ledger
from src.database.sql_connection import SQLConnection
from src.identity.node_identity import (
    get_node_public_key_hex,
    sign_peer_payload,
    statement_payload,
    verify_peer_payload,
)
from src.reputation_system.reasons import Reason
from src.utils import logger as log
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


def _sign(statement: celaut_pb2.BalanceStatement) -> celaut_pb2.BalanceStatement:
    """Sign ``statement`` with the key of this node, in place, and return it."""
    public_key = get_node_public_key_hex()
    signature = public_key and sign_peer_payload(
        statement_payload(public_key, statement_digest(statement))
    )
    # With no identity key there is nothing to sign with. The statement still goes out,
    # and its reader refuses it, as it refuses any unsigned one.
    statement.signature = signature or ""
    return statement


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
           allow_debt: bool = False, instance_ref: str = "",
           remaining_hops: Optional[int] = None) -> celaut_pb2.BalanceStatement:
    """The signed statement that ``caller`` asked for, charged to ``caller``.

    ``caller`` is who ``require_caller`` identified: a client_id, or a local instance.
    ``token`` names the account: empty for the caller's own, or a child of the caller.
    Each statement holds the rows of one account and nothing else: a child's rows are
    in the child's statement, which its father asks for by its token.

    ``instance_ref`` reads a descendant instead (#631). The node looks for that
    ``instance_ref`` below the account that ``token`` names, through the ``child_bound``
    rows of each chain (``SQLConnection.find_descendant``). The caller shows a token
    that it already holds, and never needs the token of the descendant. The descendant
    can be a stopped instance: its chain ends with the ``stop`` row. For a descendant
    below an instance that this node delegated, the node asks the peer with its own
    client_id, one hop at a time. It checks the signature of the peer, marks the
    statement ``relayed`` and signs it again: the caller checks it with the key of this
    node. The statement of a descendant gives its ``service_id`` when a node knows it.

    ``remaining_hops`` is the hop budget of the read (``RecursionGuard`` rules). None
    means the node maximum (``network.RECURSION_MAX_HOPS``). A node that asks one peer
    sends one less. A node that asks more peers shares the rest between them
    (``split_hops``). A node that gets 1 asks no peer. The depth limits of the walk are
    only for the chains of this node.

    An instance that this node delegated to a peer has no statement of its own here: its
    rows would show the delegation, and for the service nobody else runs its instance.
    A read of it with ``instance_ref`` goes to the peer, which has the real account.
    """
    account = token or caller
    # A read of a descendant may start at a parent that stopped: that is the case that
    # it exists for. Its row stays for a time after it stops.
    if account != caller and sc.father_of(account, with_dead=bool(instance_ref)) != caller:
        # The text does not repeat the token: an error is no place for a credential.
        raise StatementRefused("That token is not the account of the caller or of a child of it.")
    if instance_ref:
        return _answer_descendant(caller, account, instance_ref, int(from_seq), int(to_seq),
                                  allow_debt, _hops(remaining_hops))
    if sc.delegated_account_exists(account):
        raise StatementUnavailable("This node gives no statement of that instance.")
    return _answer_account(caller, account, int(from_seq), int(to_seq), allow_debt)


def _hops(remaining_hops: Optional[int]) -> int:
    """The hop budget of a read: the one received, never above the node maximum."""
    from src.utils.tools.recursion_guard import max_hops
    limit = max_hops()
    return limit if remaining_hops is None else min(int(remaining_hops), limit)


def _answer_account(caller: str, account: str, from_seq: int, to_seq: int,
                    allow_debt: bool, with_dead: bool = False,
                    service_id: str = "", instance_ref: str = "") -> celaut_pb2.BalanceStatement:
    """Charge ``caller``, read the rows of ``account`` and sign them.

    ``service_id`` (hex) and ``instance_ref`` go in the statement only for a read with
    ``instance_ref``: the node signs that the account is the one of that descendant.
    """
    read = sc.balance_statement(
        account, caller, from_seq=from_seq, to_seq=to_seq,
        max_rows=ledger.max_statement_rows(), price=statement_price_mu,
        allow_debt=allow_debt, with_dead=with_dead,
    )
    statement = celaut_pb2.BalanceStatement(
        account_ref=ledger.account_ref(account),
        from_seq=from_seq,
        to_seq=to_seq,
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
        service_id=_service_hex(service_id),
        instance_ref=instance_ref,
    )
    return _sign(statement)


def _service_hex(service_id) -> str:
    """``service_id`` as lowercase hex, or empty when it is not a hex text."""
    text = str(service_id or "").strip().lower()
    try:
        bytes.fromhex(text)
    except ValueError:
        return ""
    return text


# The texts that a caller of a descendant read can get. They name no peer, no address
# and no token: the caller can be a service, and a service does not know nodes.
_NOT_LINKED = "No account that the caller may read has that descendant."
_BELOW_FAILED = "A node below gave no statement."
_NO_BUDGET = "The hop budget of this read is used up."
_MANY_BELOW = "More than one node below gave a statement of that descendant."

_OUT_OF_BALANCE = frozenset(("out_of_balance", "hibernated_out_of_balance"))


def locate(root: str, instance_ref: str) -> dict:
    """Where the descendant of ``root`` that ``instance_ref`` names is (#631).

    The read of a statement and the tunnel to a descendant use the same walk. Returns
    ``{"found": row or None, "hops": [(row, ask_ref)]}``:

    - ``found`` is the descendant when this node holds it: a local instance, or the
      alias of an instance that it delegated. ``instance_ref`` can name ``root``
      itself.
    - ``hops`` are the delegated instances to ask, each with the ``instance_ref`` to
      send. For a delegated ``found``, the peer gets the ``instance_ref`` of its own
      token for the instance, so it reads or opens that instance itself, also when it
      stopped. For a descendant below a delegated instance, the peer gets the same
      ``instance_ref``: the node that linked it named it so.
    """
    if instance_ref == ledger.instance_ref(root):
        found = sc.get_instance_row(root)
    else:
        # A stopped delegated root is a root too: its peer can still hold the descendant.
        delegated_root = sc.get_delegated_instance_by_alias(root, with_dead=True)
        if delegated_root is not None:
            # The node has no chain below an instance that it delegated: the peer has it.
            return {"found": None, "hops": [(delegated_root, instance_ref)]}
        walk = sc.find_descendant(root, instance_ref, ledger.max_descendant_depth(),
                                  ledger.max_descendant_accounts())
        found = walk["found"]
        if found is None:
            return {"found": None, "hops": [(row, instance_ref) for row in walk["asked"]]}
    if found is not None and found["kind"] == "delegated":
        return {"found": found,
                "hops": [(found, ledger.instance_ref(found["token_delegation"]))]}
    return {"found": found, "hops": []}


def split_hops(hops: int, peers: int) -> list:
    """The hop budget of each peer that a node asks, for a budget of ``hops`` (#631).

    The node keeps 1 for itself. It asks at most ``hops - 1`` peers, and each peer gets
    an equal part of ``hops - 1``. One peer gets ``hops - 1``, as in ``RecursionGuard``.
    So the number of nodes that one request reaches is never more than its budget,
    also when each node has many delegated descendants.
    """
    room = int(hops) - 1
    if room <= 0 or peers <= 0:
        return []
    count = min(int(peers), room)
    return [room // count] * count


def _answer_descendant(caller: str, root: str, instance_ref: str, from_seq: int,
                       to_seq: int, allow_debt: bool,
                       hops: int) -> celaut_pb2.BalanceStatement:
    """The statement of the descendant of ``root`` that ``instance_ref`` names.

    A statement that a peer gives goes out as ``relayed`` and signed by this node: the
    caller checks it with the key of the node that it asked.

    When more than one peer can hold the descendant, the node asks all of them. A peer
    can sign a statement for any ``instance_ref``, also for a descendant that it does
    not hold, so the first answer is not proof. When more than one peer gives a
    statement, the node gives none (``_MANY_BELOW``): it cannot know which peer tells
    the truth. Each statement is still judged (``_ask_peer``), so a peer that lies does
    not hide the faults of the other one. A peer is asked only for the delegated
    descendant itself, or when it signed a link to the ref (``_linked_hops``). The
    instance that is called pays the reads to the peers (``_pay_peer_reads``), before
    the first peer is asked; the caller pays only for a peer whose instance cannot.

    The node keeps the head of the statement that it gives, for the peer that gave it
    (``SQLConnection.record_descendant_head``). A tunnel to the descendant uses that
    peer when more than one peer can hold it (``rpc_tunnel._choose_peer``).
    """
    if hops <= 0:
        raise StatementUnavailable(_NO_BUDGET)
    where = locate(root, instance_ref)
    found = where["found"]
    if found is not None and found["kind"] == "local":
        try:
            return _answer_account(caller, found["id"], from_seq, to_seq, allow_debt,
                                   with_dead=True, service_id=found.get("service_id"),
                                   instance_ref=instance_ref)
        except ledger.NoSuchAccount:
            # The retention deleted the account after the walk. The exception names the
            # account by its id, which is a token: it must not reach the caller.
            raise StatementRefused(_NOT_LINKED)
    hops_to_ask = _linked_hops(where["hops"], found)
    if not hops_to_ask:
        # No chain that this node knows links the ref: it asks no peer, and nobody pays.
        raise StatementRefused(_NOT_LINKED)
    budgets = split_hops(hops, len(hops_to_ask))
    if not budgets:
        # The node may not go into another node: the budget of the read ends here.
        raise StatementUnavailable(_NO_BUDGET)
    failed = False
    ready = []
    for (row, ask_ref), budget in zip(hops_to_ask, budgets):
        try:
            ready.append((row, ask_ref, budget, _client_on(row["peer_id"])))
        except StatementUnavailable:
            failed = True
    if not ready:
        raise StatementUnavailable(_BELOW_FAILED)
    # The instance that is called pays its own reads. One charge for all the peers, before
    # the first call to a peer.
    _pay_peer_reads(caller, root, instance_ref, from_seq, to_seq, allow_debt, ready)
    answers = []
    for row, ask_ref, budget, client_id in ready:
        try:
            statement = _ask_peer(row, client_id, ask_ref, from_seq, to_seq, budget,
                                  must_answer=_must_answer(row, ask_ref, found))
        except StatementRefused:
            continue
        except StatementUnavailable:
            # One peer that fails must not hide the descendant that another one holds.
            failed = True
            continue
        answers.append((row, ask_ref, statement))
    if len(answers) > 1:
        # A peer can sign a statement for a ref that it does not hold, once it linked
        # that ref. The honest chain showed the ref first, and a ref cannot be known
        # before it is shown, so the peer that linked it first is the one that holds it.
        first = first_linked([(row, ask_ref) for row, ask_ref, _ in answers])
        if first is None:
            log.LOGGER(f"Peers {', '.join(row['peer_id'] for row, _, _ in answers)} all gave "
                       f"a statement of descendant {instance_ref[:12]}. No statement is given.")
            raise StatementUnavailable(_MANY_BELOW)
        answers = [answers[first]]
    if not answers:
        if failed:
            raise StatementUnavailable(_BELOW_FAILED)
        raise StatementRefused(_NOT_LINKED)
    row, ask_ref, statement = answers[0]
    _keep_head(row["peer_id"], ask_ref, statement)
    if row is found and not statement.service_id:
        # A peer that does not give it: the node gives the service that it asked the
        # peer to run.
        statement.service_id = _service_hex(found.get("service_id"))
    return _relay(statement, instance_ref)


def _linked_hops(hops: list, found: Optional[dict]) -> list:
    """The peers that this node may ask for a descendant (#631).

    The delegated descendant itself (``found``) is always asked: this node started it
    there. A descendant below a delegated instance is asked only to a peer that signed a
    ``child_bound`` row of that ``instance_ref`` in a statement that it gave in the
    retention of this node (``ledger.link_horizon``, no limit by default), with
    ``SQLConnection.descendant_linked``. A ref that no peer linked is
    refused with no call: a caller cannot make this node ask its peers for a ref that it
    made up, and nobody pays for a call that was not needed.
    """
    since = ledger.link_horizon()
    return [(row, ask_ref) for row, ask_ref in hops
            if (found is not None and row is found)
            or sc.descendant_linked(row["peer_id"], ask_ref, since=since)]


def _pay_peer_reads(caller: str, root: str, instance_ref: str, from_seq: int, to_seq: int,
                    allow_debt: bool, ready: list) -> None:
    """Charge the reads that this node makes of its peers (#631).

    The instance that is called pays: for each peer, the delegated instance that this
    node holds the account of (``row["id"]``, the alias) pays one ``read_price_mu`` with a
    ``statement_query`` row that names the descendant. That is the descendant itself when
    it is the delegated one, else the delegated instance that leads to it. The caller
    pays nothing for these. The fan-out below each peer is paid by that peer, from the
    accounts that it holds, in the same way.

    A delegated instance that cannot pay (it stopped, or has no balance) leaves its read
    to the caller: one ``read_price_mu`` for each such peer, as for a stopped orphan whose
    statement is the reason for the read. Only a ref that a peer linked gets here
    (``_linked_hops``), so a made-up ref costs nobody anything.
    """
    unpaid = 0
    detail = {"descendant": instance_ref, "from_seq": str(from_seq), "to_seq": str(to_seq)}
    price = read_price_mu(from_seq, to_seq)
    for row, _, _, _ in ready:
        try:
            if not row["alive"]:
                raise ledger.NoSuchAccount(row["id"])
            sc.move_balance(row["id"], -price, ledger.STATEMENT_QUERY, allow_debt=False,
                            detail={**detail, "paid_by": "descendant"})
        except (ledger.NoSuchAccount, ledger.InsufficientBalance):
            unpaid += 1
    if unpaid:
        _charge_read(caller, root, instance_ref, from_seq, to_seq, allow_debt, peers=unpaid)


def first_linked(candidates: list) -> Optional[int]:
    """The index in ``candidates`` (pairs of a row and the ref asked) of the one peer that
    linked its ref before all the others, or None when no peer is first (#631).

    A peer that has no recorded link, or that linked at the same second as another, is not
    first. The read and the tunnel use it to choose between peers that all give a
    statement of the same descendant: a peer cannot link a ref before the chain that
    holds it has shown the ref.
    """
    dates = [sc.descendant_first_linked(row["peer_id"], ask_ref) for row, ask_ref in candidates]
    if any(date is None for date in dates):
        return None
    best = min(dates)
    return dates.index(best) if dates.count(best) == 1 else None


def _must_answer(row: dict, ask_ref: str, found: Optional[dict]) -> bool:
    """Whether the peer of ``row`` must give a statement of ``ask_ref`` (#631).

    - ``row`` is the descendant (``found``): the peer runs it for this node. It must
      keep the account, also after a stop, for the retention that a peer is held to
      (``ledger.min_peer_retention_days``).
    - A statement that the peer signed in that retention linked ``ask_ref`` with a
      ``child_bound`` row (``SQLConnection.record_descendant_links``).

    Else the peer can have no such descendant: one of more peers that this node asks.
    """
    retention = ledger.min_peer_retention_days() * 86400
    now = int(time.time())
    if row is found:
        stopped = _epoch(found.get("killed_at")) if found.get("killed_at") else None
        return stopped is None or now - stopped < retention
    return sc.descendant_linked(row["peer_id"], ask_ref, since=now - retention)


def head_conflict(peer_id: str, ask_ref: str,
                  statement: celaut_pb2.BalanceStatement) -> Optional[str]:
    """Why ``statement`` does not agree with the head of the descendant ``ask_ref`` that
    ``peer_id`` signed before and that this node kept (``SQLConnection.descendant_head``),
    or None when it agrees, or when there is no kept head (#631).

    A chain only grows: its head never goes back, a seq keeps its hash, and the row after
    the kept head hangs from it. A statement with no rows at all (``head_seq`` 0) is an
    account whose rows the retention deleted, not a conflict.
    """
    kept = sc.descendant_head(peer_id, ask_ref)
    if kept is None or not statement.head_seq:
        return None
    seq, hash_ = int(kept["head_seq"]), kept["head_hash"]
    if statement.head_seq < seq:
        return f"head {statement.head_seq} is below the head {seq} that it signed before"
    if statement.head_seq == seq and statement.head_hash != hash_:
        return f"head {seq} has another hash"
    for movement in statement.movements:
        if movement.seq == seq and movement_hash(movement) != hash_:
            return f"row {seq} has another hash"
        if movement.seq == seq + 1 and movement.prev_hash != hash_:
            return f"row {seq + 1} does not follow the head {seq}"
    return None


def _keep_head(peer_id: str, ask_ref: str, statement: celaut_pb2.BalanceStatement) -> None:
    """Keep the head of a statement that ``peer_id`` gave of a descendant. A failure to
    write is logged, and the read goes on."""
    try:
        sc.record_descendant_head(peer_id, ask_ref, statement.head_seq, statement.head_hash,
                                  ts=statement.ts or None)
    except Exception as e:
        log.LOGGER(f"Cannot keep the head of descendant {ask_ref[:12]} on {peer_id}: {e}")


def _client_on(peer_id: str) -> str:
    """The client_id of this node on ``peer_id``. ``StatementUnavailable`` if none."""
    try:
        from src.manager.manager import get_client_id_on_other_peer
        client_id = get_client_id_on_other_peer(peer_id)
    except Exception as e:
        client_id = None
        log.LOGGER(f"No client on {peer_id} for a descendant read: {e}")
    if not client_id:
        raise StatementUnavailable(_BELOW_FAILED)
    return client_id


def read_price_mu(from_seq: int, to_seq: int) -> int:
    """The price of one read that goes to a peer: the fixed part and the part for the
    most rows that the read can return."""
    rows = (to_seq - from_seq + 1) if (from_seq > 0 and to_seq >= from_seq) else None
    most_rows = 0 if from_seq == 0 else min(rows or ledger.max_statement_rows(),
                                           ledger.max_statement_rows())
    return statement_price_mu(most_rows)


def _charge_read(caller: str, root: str, instance_ref: str, from_seq: int, to_seq: int,
                 allow_debt: bool, peers: int = 1) -> None:
    """Charge ``caller`` once for a read that goes to ``peers`` peers.

    The charge is one read for each peer: the fixed part and the part for the most rows
    that the read can return (``read_price_mu``). A caller that cannot pay costs the
    node no call, and the node never pays a peer for rows that it did not charge. The
    row names the descendant by its ``instance_ref``.
    """
    own = root == caller
    rows = (to_seq - from_seq + 1) if (from_seq > 0 and to_seq >= from_seq) else None
    most_rows = 0 if from_seq == 0 else min(rows or ledger.max_statement_rows(),
                                           ledger.max_statement_rows())
    detail = {"descendant": instance_ref, "from_seq": str(from_seq), "to_seq": str(to_seq),
              "rows": str(most_rows), "peers": str(int(peers))}
    sc.move_balance(caller, -read_price_mu(from_seq, to_seq) * int(peers),
                    ledger.STATEMENT_QUERY, allow_debt=allow_debt,
                    instance_token=None if own else root, detail=detail)


def _ask_peer(row: dict, client_id: str, ask_ref: str, from_seq: int, to_seq: int,
              hops: int, must_answer: bool = False) -> celaut_pb2.BalanceStatement:
    """One hop: read from the peer of the delegated instance ``row``, as this node.

    The node uses its own client_id on that peer and the token that the peer gave it
    for the instance (``token_delegation``). The caller never sees either, and never
    needs to know that there is a peer. ``hops`` is the budget that the peer gets.

    The errors name no peer: the details go to the log of this node only.

    Each statement that the peer gives is judged here (``_judge_stop``,
    ``_judge_unreachable``), also a ``relayed`` one. The peer sets ``relayed`` and signs
    it, so this node cannot know that it is true. And the peer answers for the nodes
    that it delegates to, as this node answers for the peer to the node above it: the
    peer chose them. The ``child_bound`` rows of the statement are kept as links of the
    peer (``SQLConnection.record_descendant_links``).

    ``must_answer``: the peer holds the descendant, or signed a link to it
    (``_must_answer``). Then no statement is a fault of the peer
    (``_judge_no_statement``).
    """
    from src.manager import statement_check

    peer_id = row["peer_id"]
    rows = (to_seq - from_seq + 1) if (from_seq > 0 and to_seq >= from_seq) else None
    try:
        statement = statement_check.read(
            peer_id, client_id, token=row["token_delegation"], from_seq=from_seq,
            rows=rows, instance_ref=ask_ref, remaining_hops=hops)
    except Exception as e:
        # The detail stays in the log of the node: the caller must not learn it.
        log.LOGGER(f"{peer_id} gave no statement for a descendant read: {e}")
        if must_answer:
            _judge_no_statement(peer_id, ask_ref, e, rows or 0)
        if _is_not_found(e):
            raise StatementRefused(_NOT_LINKED)
        raise StatementUnavailable(_BELOW_FAILED)
    conflict = head_conflict(peer_id, ask_ref, statement)
    if conflict is not None:
        # Two signed chains of one descendant: a proof that the peer lied in one of them.
        # The kept head stays, and the caller gets no statement of this peer.
        log.LOGGER(f"{peer_id} signed another chain for descendant {ask_ref[:12]}: {conflict}")
        try:
            statement_check.penalise_once(
                peer_id, ask_ref, Reason.STATEMENT_INCONSISTENT,
                f"a statement of a descendant that does not agree with the one it signed "
                f"before: {conflict}", window=statement_check.FAULT_WINDOW_SECONDS)
        except Exception as e:
            log.LOGGER(f"Cannot score {peer_id} for descendant {ask_ref[:12]}: {e}")
        raise StatementUnavailable(_BELOW_FAILED)
    try:
        if must_answer:
            sc.record_descendant_read(peer_id, ask_ref, ok=True)
        sc.record_descendant_links(peer_id, (
            m.instance_ref for m in statement.movements if m.kind == ledger.CHILD_BOUND))
        _judge_stop(peer_id, statement, ask_ref)
        _judge_unreachable(peer_id, statement, ask_ref)
        _judge_receipt(peer_id, statement, ask_ref)
    except Exception as e:
        # A judgement that fails does not stop the read: the statement is valid.
        log.LOGGER(f"Cannot judge the statement of descendant {ask_ref[:12]} on {peer_id}: {e}")
    return statement


# The codes of a peer that say nothing of the statement: the network, or a node below
# the peer that did not answer (``StatementUnavailable`` is ``UNIMPLEMENTED`` on the
# wire). Such a failure is a fault only when it goes on (``_judge_no_statement``).
def _transport_codes():
    import grpc
    return (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED,
            grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.CANCELLED,
            grpc.StatusCode.UNKNOWN, grpc.StatusCode.INTERNAL)


def _judge_no_statement(peer_id: str, instance_ref: str, error: Exception,
                        rows: int) -> None:
    """Score a peer that gives no statement of a descendant that it must show (#631).

    The issue says that a refusal is already a fault: ``statement_inconsistent``. The
    peer holds the descendant, or signed a link to it (``_must_answer``).

    - A statement that does not verify (``StatementInvalid``), or a refusal of the peer
      (``PERMISSION_DENIED``, ``NOT_FOUND``, ``INVALID_ARGUMENT`` and the other codes
      that are not in ``_transport_codes``): a fault, scored once in
      ``FAULT_WINDOW_SECONDS``.
    - ``RESOURCE_EXHAUSTED``: a fault only when the balance of this node on the peer
      still pays for the read (``statement_check.still_pays``).
    - A failure of the transport, or of a node below the peer: the peer selects the
      code that it answers with, so a failure that goes on is a refusal. It is a fault
      when the first failure after the last read that worked is older than
      ``statement_check.refusal_age_seconds``.
    """
    from src.manager import statement_check

    try:
        code = _code_of(error)
        if isinstance(error, StatementInvalid) or (
                code is not None and code not in _transport_codes()):
            import grpc
            if code == grpc.StatusCode.RESOURCE_EXHAUSTED \
                    and not statement_check.still_pays(peer_id, rows):
                return
            statement_check.penalise_once(
                peer_id, instance_ref, Reason.STATEMENT_INCONSISTENT,
                f"no statement of a descendant that it must show: {error}",
                window=statement_check.FAULT_WINDOW_SECONDS)
            return
        since = sc.record_descendant_read(peer_id, instance_ref, ok=False)
        if since is not None and time.time() - since >= statement_check.refusal_age_seconds():
            statement_check.penalise_once(
                peer_id, instance_ref, Reason.STATEMENT_INCONSISTENT,
                f"no statement of a descendant that it must show since {since}: {error}",
                window=statement_check.FAULT_WINDOW_SECONDS)
    except Exception as e:
        log.LOGGER(f"Cannot judge the refusal of {peer_id} for descendant "
                   f"{instance_ref[:12]}: {e}")


def _relay(statement: celaut_pb2.BalanceStatement,
           instance_ref: str) -> celaut_pb2.BalanceStatement:
    """The statement of a peer, as this node gives it to its caller.

    This node checked the signature of the peer, and that the peer signed the
    ``instance_ref`` that this node asked for. It marks the statement ``relayed``, sets
    the ``instance_ref`` that its own caller asked for (``instance_ref``), and signs it
    with its own key: the caller knows only this node, and a node above checks the
    statement with the key of the node that it asked. The rows and their hashes do not
    change, so the chain still shows any change that the peer made.
    """
    relayed = celaut_pb2.BalanceStatement()
    relayed.CopyFrom(statement)
    relayed.relayed = True
    relayed.instance_ref = instance_ref
    return _sign(relayed)


# A stop row whose ``detail.tick`` is more than this many times the largest maintenance
# row of the statement uses this limit in place of ``tick``. The scarcity of a node can
# make a tick cost more than its past rows, but not without a limit.
_MAX_TICK_FACTOR = 10


def _judge_stop(peer_id: str, statement: celaut_pb2.BalanceStatement,
                instance_ref: str) -> None:
    """Score the peer that killed an orphan descendant while it had MU (#631).

    This node records the fault by itself. The service that reads only sees the
    statement. The fault is a signed ``stop`` row with ``detail.leftover`` above 0 whose
    ``refund`` row (the row before it) says ``detail.lost = father_gone``: the father of
    the descendant was gone, nobody could judge the peer, and the MU stayed with it. A
    father that stopped its child got the MU, so that stop is not a fault.

    An orphan stops only when its MU end. Nobody can stop it by the API: only its father
    holds the token, and the father is gone. So the reason that the peer writes does not
    matter: a VM that went down is the peer's to start again, and a machine that failed
    is the fault of its operator. Only this stop is not scored:

    - ``out_of_balance`` with less MU left than the price of the tick that the
      instance could not pay. The stop row gives that price (``detail.tick``), at the
      scarcity of the node at that time. A ``tick`` above ``_MAX_TICK_FACTOR`` times the
      largest maintenance row is not believed: that limit is used. A stop row with no
      ``tick`` uses the largest maintenance row, and with no maintenance row in the
      statement it is not judged.

    The fault is scored once for each descendant and row. A statement that does not
    show the refund row is not judged.
    """
    from src.manager import statement_check

    rows = statement_check.rows_of(statement)
    lost = {row.seq for row in rows
            if row.kind == ledger.REFUND and row.amount < 0
            and row.detail.get("lost") == "father_gone"}
    period = max((-row.amount for row in rows
                  if row.kind == ledger.MAINTENANCE and row.amount < 0), default=0)
    for row in rows:
        if row.kind != ledger.STOP or (row.seq - 1) not in lost:
            continue
        leftover = _int_of(row.detail.get("leftover"))
        reason = str(row.detail.get("reason", ""))
        if leftover <= 0:
            continue
        if reason in _OUT_OF_BALANCE:
            tick = _int_of(row.detail.get("tick")) if "tick" in row.detail else None
            if tick is None:
                tick = period
            elif period:
                tick = min(tick, period * _MAX_TICK_FACTOR)
            if not tick or leftover < tick:
                continue
        statement_check.penalise_once(
            peer_id, instance_ref, Reason.KILLED_WITH_MU,
            f"stop row {row.seq} ({reason}) with {leftover} MU left and the father gone",
            row.seq)


def _int_of(value) -> int:
    """``value`` as an int, 0 when it is not a number."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _judge_unreachable(peer_id: str, statement: celaut_pb2.BalanceStatement,
                       instance_ref: str) -> None:
    """Score the peer that charges maintenance of a descendant that does not answer.

    ``DescendantTunnel`` hops of this node to ``peer_id`` for ``instance_ref`` failed
    with the descendant unavailable, and no hop worked after the first of them
    (``SQLConnection.descendant_tunnel_failure``: the first and the last failure). These
    failures are not recorded: a refusal of authorization, a slot that the service does
    not declare, and a hop budget that is used up.

    The fault ``maintenance_while_unreachable`` is a ``maintenance`` row that charges
    memory for a period between two failures: the period starts more than
    ``CLOCK_SKEW_SECONDS`` after the first failure and ends more than
    ``CLOCK_SKEW_SECONDS`` before the last one. The period of a row starts
    ``detail.seconds`` before its date. One failure is not enough: the instance can
    answer again after it (a hibernated instance that its father wakes, or a service
    that did not listen on its slot for a short time). With a second failure after the
    period, the instance did not answer before or after that period.

    These rows are not scored:

    - a row whose period starts before the first failure: an honest node can charge the
      period in which the VM stopped and write the ``stop`` row at the next tick;
    - a row that charges no memory (``detail.mem_limit`` 0): it is the disk of a
      hibernated instance, which has no VM;
    - all the rows, when a row with no memory charges a period between the two
      failures: the instance hibernated, so the failures can be of the hibernation;
    - a row after a ``stop`` row.

    The fault is scored once for each peer and descendant. The problem in the log of
    the node gives the seq of the first row that shows it.

    This judgement is local to this node. A failed tunnel is not a signed proof.
    """
    from src.manager import statement_check

    failure = sc.descendant_tunnel_failure(peer_id, instance_ref)
    if failure is None:
        return
    first, last = failure
    skew = statement_check.CLOCK_SKEW_SECONDS
    rows = statement_check.rows_of(statement)
    stopped = min((row.ts for row in rows if row.kind == ledger.STOP), default=None)
    between = [row for row in rows
               if row.kind == ledger.MAINTENANCE and row.amount < 0
               and (stopped is None or row.ts <= stopped)
               and _period_start(row) > first + skew and row.ts < last - skew]
    if any(_no_memory(row) for row in between):
        return
    for row in between:
        statement_check.penalise_once(
            peer_id, instance_ref, Reason.MAINTENANCE_WHILE_UNREACHABLE,
            f"maintenance row {row.seq} charges a period between tunnels that failed "
            f"at {first} and at {last}", _ONCE_FOR_EACH_DESCENDANT)
        return


def _judge_receipt(peer_id: str, statement: celaut_pb2.BalanceStatement,
                   instance_ref: str) -> None:
    """Score the peer whose statement contradicts the receipt that it signed (#419).

    A ``DescendantTunnel`` hop of this node to ``peer_id`` for ``instance_ref`` keeps the
    last valid receipt of the peer (``SQLConnection.descendant_receipt``). The rows of the
    statement are checked against it with ``receipt_statement.check``, the check that
    ``nodo receipt verify --statement`` prints. Two signatures of one peer that disagree
    are a proof, so the fault is scored once for each peer, descendant and row
    (``Reason.STATEMENT_CONTRADICTS_RECEIPT``). A statement that does not hold the rows of
    the session is not judged.
    """
    from src.identity import receipt_statement
    from src.manager import statement_check

    stored = sc.descendant_receipt(peer_id, instance_ref)
    if not stored:
        return
    receipt = celaut_pb2.ExecutionReceipt()
    receipt.ParseFromString(stored)
    report = receipt_statement.check(
        [receipt], statement_check.rows_of(statement), peer_id=peer_id)
    for finding in report["findings"]:
        statement_check.penalise_once(
            peer_id, instance_ref, Reason.STATEMENT_CONTRADICTS_RECEIPT,
            f"{finding['kind']}: {finding['detail']}", int(finding["seq"]))


def _no_memory(row) -> bool:
    """Whether a ``maintenance`` row charges no memory: a hibernated instance."""
    return str(row.detail.get("mem_limit", "")) == "0"


# The seq of the fault record of ``maintenance_while_unreachable``. It is the same for
# every row, so the fault is scored once for each peer and descendant.
_ONCE_FOR_EACH_DESCENDANT = 0


def _period_start(row) -> int:
    """The date when the period of a ``maintenance`` row starts: its date less
    ``detail.seconds``. A row with no valid ``seconds`` starts at its date."""
    return int(row.ts) - max(0, _int_of(row.detail.get("seconds")))


def _epoch(text) -> Optional[int]:
    """Seconds since the epoch of a ``CURRENT_TIMESTAMP`` text (UTC), None if it is not one."""
    import calendar
    try:
        return calendar.timegm(time.strptime(str(text), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None


def _code_of(error: Exception):
    """The gRPC status code of ``error``, or None."""
    code = getattr(error, "code", None)
    try:
        return code() if callable(code) else None
    except Exception:
        return None


def _is_not_found(error: Exception) -> bool:
    """Whether ``error`` is a gRPC refusal that says the account is not there."""
    import grpc
    return _code_of(error) in (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.NOT_FOUND)


def fetch(peer_id: str, client_id: str, token: str = "", from_seq: int = 0,
          to_seq: int = 0, timeout: Optional[float] = None,
          instance_ref: str = "",
          remaining_hops: Optional[int] = None) -> celaut_pb2.BalanceStatement:
    """Read a statement from ``peer_id`` as ``client_id``, and check its signature.

    ``token`` is a child that this node delegated there, by the token of the peer.
    Raises ``StatementInvalid`` for no answer, an answer about another account or
    range, or a signature that does not verify. A transport error is raised as it is,
    with its status code. ``timeout`` is how long the call may take, in seconds.
    """
    from src.identity.grpc_transport import peer_channel
    from src.utils.bee_client import BeeClient

    statement = BeeClient.get_balance_statement(
        peer_channel(peer_id=peer_id), client_id=client_id, token=token,
        from_seq=from_seq, to_seq=to_seq, timeout=timeout, instance_ref=instance_ref,
        remaining_hops=remaining_hops,
    )
    if statement is None:
        raise StatementInvalid(f"{peer_id} answered no statement.")
    # A descendant has an account that this node does not know by id: the peer signs the
    # instance_ref that was asked for in its place (#631). Without it, the peer could say
    # later that the statement was of another account.
    if (not instance_ref
            and statement.account_ref != ledger.account_ref(token or client_id)) \
            or statement.instance_ref != instance_ref \
            or statement.from_seq != from_seq or statement.to_seq != to_seq:
        raise StatementInvalid(f"{peer_id} answered a statement that was not asked for.")
    if not verify(statement, peer_id):
        raise StatementInvalid(f"The statement from {peer_id} is not signed by it.")
    return statement

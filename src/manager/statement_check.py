"""Checking a peer from its signed statements (#570).

A balance cannot tell a wrong charge from a legitimate one: it is the sum of every
movement, and many movements move it. A statement is a chain of movements that the peer
signed, and this node reads the statement of its own account on a peer, and of each child
that it delegated there.

Of its own account it checks two things:

1. It continues the mark (``verify_chain``): the mark is the last movement that this node
   checked, and each new movement hangs from the hash of the one before it, adds to the
   balance, and ends in the head that the peer signed. A peer that shows other movements
   than it showed before cannot keep the chain.
2. Nothing takes MU from it that this node did not cause: the start of its own children
   (each one is written down before the peer is asked), their top-ups up to what was sent,
   and the statements that it asked for.

Of a child it checks the chain, and nothing else. What the peer charges a child is the
business of whoever runs the child, and this node only charges its father what the child
consumed, which the movements say (``consumption``).

A peer whose statement does not continue the chain, or that charges our account what
this node did not cause, loses ``STATEMENT_PENALTY``: the same as a service that does
not hash to its id, because in every case the peer signed something false about money.

The functions here are pure unless they say otherwise; ``maintain`` and
``delegate_execution`` read the statements and act on what these return.
"""
import json
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

import grpc

from protos import celaut_pb2
from src.database import ledger
from src.reputation_system.reasons import Reason
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.utils import from_amount

env_manager = ConfigManager()

STATEMENT_PENALTY = -1000

# Room for the clocks: a ts is whole seconds, and a tick may come a moment early.
CLOCK_SLACK_SECONDS = 2

# How far the clock of a peer may be from this node's: a statement dated further away is
# not valid, so a peer cannot choose the time that its own movements are judged at.
CLOCK_SKEW_SECONDS = 120

@dataclass(frozen=True)
class Row:
    seq: int
    ts: int
    amount: int
    balance_after: int
    kind: str
    ref: str
    detail: Mapping = field(default_factory=dict)
    hash: str = ""


@dataclass(frozen=True)
class Mark:
    """Where this node has checked an account up to.

    ``seq`` and ``hash`` are the last movement that was checked, and ``balance`` the
    balance after it. ``ts`` is a time, on the clock of the peer, before which no
    movement after ``seq`` was written: the ``ts`` of that movement, or the ``ts`` of
    the statement that showed it to be the head. ``seq`` None: nothing checked yet.
    """
    seq: Optional[int]
    hash: str
    balance: int
    ts: Optional[int] = None

    @staticmethod
    def new_account(account: str, ts: int) -> "Mark":
        """The mark of an account that was just created: before its movement 1."""
        return Mark(0, ledger.genesis_hash(account), 0, int(ts))


NO_MARK = Mark(None, "", 0)


def tick_detail(sys_req, seconds: int) -> dict:
    """The cause of a maintenance period: what it was priced for, and for how long."""
    return {
        "seconds": int(seconds),
        "mem_limit": int(sys_req["mem_limit"] or 0),
        "disk_space": int(sys_req["disk_space"] or 0),
        "cpu_period": int(sys_req["cpu_period"] or 0),
        "cpu_quota": int(sys_req["cpu_quota"] or 0),
    }


def _detail(text: str) -> Mapping:
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def rows_of(statement: celaut_pb2.BalanceStatement) -> List[Row]:
    return [
        Row(seq=int(m.seq), ts=int(m.ts), amount=from_amount(m.amount),
            balance_after=from_amount(m.balance_after), kind=m.kind,
            ref=m.instance_ref, detail=_detail(m.detail), hash=_hash_of(m))
        for m in statement.movements
    ]


def _hash_of(movement: celaut_pb2.BalanceMovement) -> str:
    return ledger.movement_hash(
        movement.prev_hash, movement.seq, movement.ts, movement.amount.n,
        movement.balance_after.n, movement.kind, movement.instance_ref, movement.detail,
    )


@dataclass(frozen=True)
class Chain:
    """What ``verify_chain`` found in a statement.

    ``rows`` are the movements after the mark that were checked, in order: None when the
    statement is not valid, and ``problem`` says why and ``fault_seq`` where. ``more``:
    the node holds movements after the last one, which the next read asks for. ``gap``:
    the statement starts after movements that the node had deleted, which its
    retention excuses (the movements between are not checkable).
    """
    rows: Optional[List[Row]]
    problem: Optional[str] = None
    fault_seq: Optional[int] = None
    more: bool = False
    gap: bool = False

    @property
    def reaches_head(self) -> bool:
        return self.rows is not None and not self.more


def _canonical(amount) -> bool:
    return isinstance(amount.n, str) and amount.n == str(int(amount.n))


def verify_chain(statement: celaut_pb2.BalanceStatement, mark: Mark, *, now: float,
                 from_seq: int) -> Chain:
    """The movements after ``mark``, if the statement is a valid continuation of it.

    ``from_seq`` is what the read asked for (``mark.seq + 1``, or 1 with no mark). The
    signature is checked elsewhere (``balance_statement.fetch``); this checks the
    content, and a node that fails it signed something false:

    1. The head is not behind the mark, is the mark when nothing came after it, and is
       dated near the clock of this node.
    2. The movements are consecutive, each one hangs from the hash of the one before and
       adds to the balance, and none is dated after the statement.
    3. The first movement of an account hangs from its genesis.
    4. When the last movement is the head, it is the head: its hash and its balance.
    5. The first movement joins the mark: the next seq, hanging from its hash. When it
       does not, the movements between were deleted by the retention of the node, and
       that is excused only when the mark is older than the retention. The node returns
       from ``from_seq``, or from the first movement it holds if that is later, and
       never skips some of what it holds.
    """
    rows = rows_of(statement)
    balance = from_amount(statement.balance)
    first = from_seq

    def invalid(problem, seq):
        return Chain(None, problem, seq)

    if abs(statement.ts - now) > CLOCK_SKEW_SECONDS:
        return invalid(f"the statement is dated {statement.ts}, and it is {int(now)}", statement.head_seq)
    if mark.seq is not None and statement.head_seq < mark.seq:
        return invalid(f"the head went back from movement {mark.seq} to {statement.head_seq}",
                       mark.seq)
    if not all(_canonical(m.amount) and _canonical(m.balance_after) for m in statement.movements) \
            or not _canonical(statement.balance):
        return invalid("an amount is not a canonical integer", first)

    for previous, row in zip(rows, rows[1:]):
        if row.seq != previous.seq + 1:
            return invalid(f"movements {previous.seq + 1} to {row.seq - 1} are missing", row.seq)
    previous_hash, previous_row = None, None
    for movement, row in zip(statement.movements, rows):
        if previous_row is not None:
            if movement.prev_hash != previous_hash:
                return invalid(f"movement {row.seq} does not hang from movement {previous_row.seq}", row.seq)
            if previous_row.balance_after + row.amount != row.balance_after:
                return invalid(f"movement {row.seq} does not add up", row.seq)
        if row.ts > statement.ts:
            return invalid(f"movement {row.seq} is dated after the statement", row.seq)
        previous_hash, previous_row = row.hash, row

    gap = False
    if rows:
        head = rows[0]
        if head.seq == 1:
            genesis = ledger.genesis_hash_of_ref(statement.account_ref)
            if statement.movements[0].prev_hash != genesis:
                return invalid("the first movement does not hang from the genesis of the account", 1)
            if head.amount != head.balance_after:
                return invalid("the first movement does not start from 0", 1)
        expected_first = max(first, statement.oldest_seq)
        if head.seq != expected_first:
            return invalid(f"movements {expected_first} to {head.seq - 1} are held and not shown",
                           head.seq)
        if mark.seq is not None:
            if head.seq <= mark.seq:
                return invalid(f"movement {head.seq} was not asked for", head.seq)
            if head.seq == mark.seq + 1:
                if statement.movements[0].prev_hash != mark.hash:
                    return invalid(
                        f"movement {mark.seq} is not the movement that it was", head.seq)
                if mark.balance + head.amount != head.balance_after:
                    return invalid(
                        f"movement {head.seq} does not add up from movement {mark.seq}", head.seq)
            else:
                retention = max(int(statement.retention_days), ledger.min_peer_retention_days())
                if mark.ts is not None and mark.ts >= now - retention * 86400 - CLOCK_SKEW_SECONDS:
                    return invalid(
                        f"movements {mark.seq + 1} to {head.seq - 1} were deleted before "
                        f"{retention} days", mark.seq + 1)
                gap = True
        elif head.seq > 1:
            gap = True
    if rows and rows[-1].seq == statement.head_seq:
        if rows[-1].hash != statement.head_hash:
            return invalid("the hash of the last movement is not the head", rows[-1].seq)
        if rows[-1].balance_after != balance:
            return invalid("the balance is not the balance after the last movement", rows[-1].seq)
    elif not rows:
        if statement.head_seq == 0:
            if balance != 0 or statement.head_hash != ledger.genesis_hash_of_ref(statement.account_ref):
                return invalid("an account with no movement holds a balance", 0)
        elif mark.seq is not None and statement.head_seq == mark.seq:
            if statement.head_hash != mark.hash or balance != mark.balance:
                return invalid(f"movement {mark.seq} is not the movement that it was", mark.seq)
    elif rows[-1].seq > statement.head_seq:
        return invalid("a movement is after the head", rows[-1].seq)
    more = statement.head_seq > (rows[-1].seq if rows else (mark.seq or 0))
    return Chain(rows, None, None, more=more, gap=gap)


def mark_after(chain: Chain, statement: celaut_pb2.BalanceStatement, mark: Mark) -> Mark:
    """The mark once ``chain`` was accepted: at its last movement, or still at ``mark``."""
    if chain.rows:
        last = chain.rows[-1]
        return Mark(last.seq, last.hash, last.balance_after,
                    statement.ts if chain.reaches_head else last.ts)
    if mark.seq is not None and not chain.more:
        return Mark(mark.seq, mark.hash, mark.balance, statement.ts)
    return mark


def anchor_at_head(statement: celaut_pb2.BalanceStatement) -> Mark:
    """A mark at the head of the account, from what the statement says of it.

    For a statement that cannot be checked from the mark: the movements up to the head
    cannot be, and asking for them again would only fail again.
    """
    return Mark(int(statement.head_seq), statement.head_hash, from_amount(statement.balance),
                int(statement.ts))


def advertised_rates(peer: Optional[celaut_pb2.Peer]) -> Dict[str, int]:
    """The rates in the ``Peer`` of a peer (``Peer.mu_per_call``)."""
    from src.utils import keyvalue

    if peer is None:
        return {}
    rates = {}
    for key, amount in keyvalue.items(peer.mu_per_call):
        try:
            rates[key] = int(amount.n)
        except (TypeError, ValueError):
            continue
    return rates


def stored_rates(peer_id: str) -> Dict[str, int]:
    """The rates in the last ``Peer`` that this node stored for ``peer_id``."""
    from src.database.sql_connection import SQLConnection

    raw = SQLConnection().get_peer_advertisement(peer_id=peer_id)
    if not raw:
        return {}
    peer = celaut_pb2.Peer()
    try:
        peer.ParseFromString(raw)
    except Exception:
        return {}
    return advertised_rates(peer)


def caused_by_father(row: Row) -> bool:
    """The child's side of a ModifyDeposit of its father: not consumption."""
    return (row.kind == ledger.TOP_UP and row.amount > 0) or \
        (row.kind == ledger.WITHDRAWAL and row.amount < 0)


def opens_account(row: Row) -> bool:
    """What opened the child's account: the amount it was started with, and the build
    that the peer took from it. Neither is consumption, and the build is what this node
    already paid at the start (``delegate_execution``)."""
    return row.kind in (ledger.START, ledger.BUILD)


def consumption(rows: Iterable[Row]) -> int:
    """What the child spent in ``rows``, in the MU of the peer: every movement but
    those of its father and those that opened its account. Never below 0."""
    return max(0, -sum(row.amount for row in rows
                       if not caused_by_father(row) and not opens_account(row)))


def father_net(rows: Iterable[Row]) -> int:
    """The top-ups of the father in ``rows`` less its withdrawals, in the MU of the peer
    and signed as the child sees them: a top-up is positive."""
    return sum(row.amount for row in rows if caused_by_father(row))


def start_grace_seconds() -> int:
    """How long a start may take on the peer: the movement that pays for it is dated
    up to this long after this node asked for it."""
    timeout = int(env_manager.get("START_SERVICE_ON_PEER_TIMEOUT", 0) or 0)
    iteration = int(env_manager.get("MANAGER_ITERATION_TIME", 10) or 10)
    return (timeout if timeout > 0 else 600) + 2 * iteration


@dataclass(frozen=True)
class StartAttempt:
    """A start that this node asked the peer for and that has no child of ours: the peer
    may have finished it after this node gave up. ``sent_mu`` is the most that it may
    take for it, in the MU of the peer."""
    id: int
    ts: int
    sent_mu: int


@dataclass(frozen=True)
class AccountCheck:
    """What ``account_faults`` found in the movements of our account.

    ``faults`` are the problems, each with the seq where it shows. ``attempts_used`` are
    the start attempts that a ``child_start`` was matched with, and ``top_ups_taken`` is
    what the peer took for the top-ups of each child, by ``instance_ref``: both are
    saved with the mark, so that a movement is accounted for once.
    """
    faults: List[Tuple[int, str]]
    attempts_used: List[int] = field(default_factory=list)
    top_ups_taken: Dict[str, int] = field(default_factory=dict)


def account_faults(rows: Iterable[Row], starts: Mapping[str, int],
                   attempts: Iterable[StartAttempt], top_up_allowances: Mapping[str, int],
                   now: float) -> AccountCheck:
    """What the peer took from our account that this node did not cause.

    A start is written down before the peer is asked (``attempts``), so every
    ``child_start`` that the peer writes can be told from one that nobody asked for the
    moment it shows, and nothing waits:

    * a child of ours (``starts``, by the ``instance_ref`` of its token on the peer, with
      what was sent for it) is named by the ``child_bound`` row that follows its start,
      and may take up to what was sent;
    * a start with no child of ours (the peer finished one that timed out, or has not
      named it) is matched with an attempt that has no child and was not matched yet: it
      was written at most ``start_grace_seconds`` before the movement, and it took no more
      than what that attempt sent. One attempt pays for one movement;
    * a top-up is for a child of ours, and takes no more than this node sent for it
      (``top_up_allowances``, which the caller raises before the peer is asked).

    A peer cannot take MU from our account under a name that matches none of these.
    The attempt window is on the clock of this node, never the date of the statement:
    a peer chooses that.
    """
    rows = list(rows)
    faults: List[Tuple[int, str]] = []
    refunded = set()
    bound = {}
    for row in rows:
        if row.kind == ledger.REFUND and row.amount > 0:
            try:
                refunded.add(int(row.detail["refunds_seq"]))
            except (KeyError, TypeError, ValueError):
                pass
        elif row.kind == ledger.CHILD_BOUND:
            try:
                bound[int(row.detail["starts_seq"])] = row.ref
            except (KeyError, TypeError, ValueError):
                pass
    open_attempts = list(attempts)
    used: List[int] = []
    allowance = dict(top_up_allowances)
    taken: Dict[str, int] = {}
    window = start_grace_seconds() + CLOCK_SKEW_SECONDS
    for row in rows:
        if row.amount >= 0 or row.kind == ledger.STATEMENT_QUERY:
            continue
        if row.kind == ledger.CHILD_START:
            if row.seq in refunded:
                continue
            child = bound.get(row.seq, "")
            if child in starts:
                if -row.amount > starts[child]:
                    faults.append((
                        row.seq,
                        f"the start of {child[:8]} took {-row.amount} MU, and "
                        f"{starts[child]} were sent",
                    ))
                continue
            match = next((a for a in open_attempts
                          if a.ts - CLOCK_SKEW_SECONDS <= row.ts <= a.ts + window
                          and -row.amount <= a.sent_mu), None)
            if match is None:
                faults.append((
                    row.seq,
                    f"movement {row.seq} pays the start of a child that this node did not "
                    f"ask for ({child[:8] or 'no child'}), {-row.amount} MU"))
                continue
            open_attempts.remove(match)
            used.append(match.id)
        elif row.kind == ledger.TOP_UP:
            if row.ref not in allowance:
                faults.append((row.seq, f"movement {row.seq} tops up an instance that is not ours"))
                continue
            allowed = allowance[row.ref]
            if -row.amount > allowed:
                faults.append((
                    row.seq,
                    f"movement {row.seq} tops up {row.ref[:8]} by {-row.amount} MU, and "
                    f"{allowed} were sent"))
            paid = min(-row.amount, allowed)
            allowance[row.ref] = allowed - paid
            taken[row.ref] = taken.get(row.ref, 0) + paid
        else:
            faults.append((
                row.seq, f"movement {row.seq} is a {row.kind!r} charge that this node did not cause"
            ))
    return AccountCheck(faults, used, taken)


def stale_mark(mark_ts: Optional[int], now: float) -> bool:
    """Whether the mark of an account is old enough to read it again, when there is no
    child to read it for: a tenth of the retention that a peer is held to, so that the
    mark is never old enough to excuse movements that a peer hides (``verify_chain``)."""
    return mark_ts is None or now - mark_ts >= ledger.min_peer_retention_days() * 86400 / 10


def unreachable(error: Exception) -> bool:
    """A transport failure, which says nothing about the statement of the peer."""
    return _code_of(error) in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED)


def _code_of(error: Exception):
    return error.code() if isinstance(error, grpc.RpcError) and callable(getattr(error, "code", None)) \
        else None


def page_rows(balance_mu: int, rates: Mapping[str, int]) -> int:
    """How many movements a read may ask for with ``balance_mu`` on the peer, at its
    rates. 0 when it could not pay for even the head of the account.

    The peer charges its fixed part and a part for each movement, and for one more: the
    charge of the read, which our own account shows. A peer that refuses a statement
    that this much covers is at fault, so the read asks for no more than it can pay.
    """
    from src.utils.cost_functions.general_cost_functions import (
        RATE_STATEMENT_QUERY,
        RATE_STATEMENT_ROW,
    )

    query = int(rates.get(RATE_STATEMENT_QUERY, 0))
    row = int(rates.get(RATE_STATEMENT_ROW, 0))
    cap = ledger.max_statement_rows()
    if row <= 0:
        return cap if balance_mu >= query else 0
    affordable = (int(balance_mu) - query) // row - 1
    return max(0, min(cap, affordable))


def read(peer_id: str, client_id: str, *, token: str = "", from_seq: int,
         rows: Optional[int] = None) -> celaut_pb2.BalanceStatement:
    """``balance_statement.fetch``, with the books of what the peer may charge for it.

    ``rows`` is how many movements the read asks for (None: the node decides). Our
    account pays for every read, and the peer writes each charge in it, so each read is
    counted when it is asked, before the peer can charge it: a read that fails after the
    charge still has its row. The rows that it may be priced for are the one of its own
    charge, and then those of the answer. A read that fails is written down with its
    range and time, at the rows asked for, and ``settle_failed_reads`` prices it later
    with the rows that the account had then. ``from_seq`` 0 asks for the head only: no
    movement.
    """
    from src.database.sql_connection import SQLConnection
    from src.manager import balance_statement

    sc = SQLConnection()
    sc.add_statement_read(peer_id, rows_priced=1)
    asked = rows or ledger.max_statement_rows()
    to_seq = from_seq + rows - 1 if rows else 0
    try:
        statement = balance_statement.fetch(
            peer_id, client_id, token=token, from_seq=from_seq, to_seq=to_seq)
    except Exception:
        if from_seq > 0:
            sc.add_failed_statement_read(
                peer_id, ledger.account_ref(token or client_id), from_seq,
                from_seq + asked - 1, asked)
        raise
    sc.add_statement_read(peer_id, rows_priced=len(statement.movements), count=0)
    return statement


def priced_failed_reads(pending: Iterable[Tuple[int, int, int, int]], rows: Iterable[Row],
                        head_seq: int) -> Dict[int, int]:
    """The reads that failed (``(id, from_seq, to_seq, ts)``) that ``rows``, the movements
    of the account that were just checked up to ``head_seq``, show the range of, with the
    rows that they may be priced for: those that the range held when the read was asked.

    A read whose range the movements do not cover is left out: it stays at the rows asked
    for. Rows are as old as the peer says, and a peer that dates them earlier makes the
    count lower, which can only cost it.
    """
    rows = list(rows)
    priced = {}
    for read_id, from_seq, to_seq, ts in pending:
        last = min(to_seq, head_seq)
        if from_seq > head_seq:
            priced[read_id] = 0
        elif rows and rows[0].seq <= from_seq and rows[-1].seq >= last:
            priced[read_id] = sum(1 for row in rows if from_seq <= row.seq <= last
                                  and row.ts <= ts + CLOCK_SKEW_SECONDS)
    return priced


def settle_failed_reads(peer_id: str, account: str, rows: Iterable[Row], head_seq: int) -> None:
    """Price the failed reads of ``account`` that the checked movements show the range of."""
    from src.database.sql_connection import SQLConnection

    sc = SQLConnection()
    pending = sc.failed_statement_reads(peer_id, ledger.account_ref(account))
    priced = priced_failed_reads(pending, rows, head_seq) if pending else {}
    if priced:
        sc.settle_failed_statement_reads(peer_id, priced)


def query_charge_problems(rows: Iterable[Row], counters: Mapping[str, int],
                          rates: Mapping[str, int]) -> List[str]:
    """Whether the peer charged our account for more statements than we asked for.

    ``counters`` are the totals that ``SQLConnection.get_statement_counters`` gives,
    with ``rows`` already added to the verified ones. What the peer showed never exceeds
    what we asked for, in number and in MU: asked for, at the fixed part of the rates
    and a part for each movement that the reads may be priced for.
    """
    from src.utils.cost_functions.general_cost_functions import (
        RATE_STATEMENT_QUERY,
        RATE_STATEMENT_ROW,
    )

    rows = [row for row in rows if row.kind == ledger.STATEMENT_QUERY and row.amount < 0]
    if not rows:
        return []
    allowed_mu = (counters["reads"] * int(rates.get(RATE_STATEMENT_QUERY, 0))
                  + counters["rows_priced"] * int(rates.get(RATE_STATEMENT_ROW, 0)))
    problems = []
    if counters["verified"] > counters["reads"]:
        problems.append(
            f"{counters['verified']} statement charges, and {counters['reads']} statements were asked for"
        )
    if counters["verified_mu"] > allowed_mu:
        problems.append(
            f"statement charges of {counters['verified_mu']} MU, and what was asked for "
            f"costs {allowed_mu} MU at its rates"
        )
    return problems


def still_pays(peer_id: str, rows_asked: int) -> bool:
    """Whether our balance on ``peer_id``, read again now, still pays for a read of
    ``rows_asked`` movements. False when it cannot be read."""
    from src.manager.metrics import refresh_balance_on_peer

    try:
        balance = refresh_balance_on_peer(peer_id)
    except Exception:
        return False
    return page_rows(balance, stored_rates(peer_id)) >= rows_asked


def penalise_no_statement(peer_id: str, account: str, error: Exception, seq: int = 0,
                          child: bool = False, pays=None) -> None:
    """A peer that gives no valid statement is one whose statement does not add up.

    Not when the peer cannot be reached: that says nothing about its statements. Nor
    when ``child`` and the peer answers ``UNIMPLEMENTED``: it may have delegated the
    child on, and the proto lets a node say so (the child is then charged from its
    balance, as with no statement). ``RESOURCE_EXHAUSTED`` is a refusal like any other
    when ``pays()`` says that our balance, read again, still pays for what was asked
    (``page_rows``): a peer that says we cannot pay for it contradicts the balance that
    it reports. When the balance fell in between, it is not a fault. ``seq`` is the
    movement where the statement was missing.
    """
    code = _code_of(error)
    if unreachable(error) or (child and code == grpc.StatusCode.UNIMPLEMENTED):
        return
    if code == grpc.StatusCode.RESOURCE_EXHAUSTED and pays is not None and not pays():
        return
    # A refusal shows no movement, and it would be the same fault at every read for as
    # long as the peer keeps refusing: it is scored once a day instead.
    penalise_once(peer_id, account, Reason.STATEMENT_INCONSISTENT,
                  f"no valid statement: {error}", seq, period=int(time.time()) // 86400)


def _invalid_statement():
    from src.manager.balance_statement import StatementInvalid
    return StatementInvalid


def penalise_once(peer_id: str, account: str, reason: str, problem: str, seq: int = 0,
                  period: int = 0) -> None:
    """Score a fault of a peer, once: the same fault read again costs it nothing.

    A fault is the peer, the account (by ``account_ref``, never the credential), the
    reason and the seq of the movement where it shows, and the ``period`` for a fault
    that shows no movement (``statement_faults``). A
    statement that does not add up stays wrong for every read until the mark goes past
    it, and also after a restart, so the record is in the database. A different fault,
    at another movement, is scored.
    """
    from src.database.sql_connection import SQLConnection
    from src.reputation_system import interface

    account_ref = ledger.account_ref(account)
    log.LOGGER(f"Statement of {account_ref[:8]} on {peer_id}: {problem}")
    if not SQLConnection().record_statement_fault(peer_id, account_ref, reason, seq, period):
        return
    interface.update_peer_reputation(peer_id=peer_id, amount=STATEMENT_PENALTY, reason=reason)

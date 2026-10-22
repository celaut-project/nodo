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
from dataclasses import dataclass, field, replace
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

# A fault that shows no movement is scored at most once in this window (seconds). The
# window starts when the fault is scored, so it does not depend on the date in UTC.
FAULT_WINDOW_SECONDS = 86400

# The seq that a statement dated far from our clock is scored at: it shows no movement.
CLOCK_FAULT_SEQ = -1

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
    retention excuses (the movements between are not checkable). ``clock``: the
    statement is refused because it is dated far from our clock. That shows no
    movement, and the clock that is wrong can be ours.
    """
    rows: Optional[List[Row]]
    problem: Optional[str] = None
    fault_seq: Optional[int] = None
    more: bool = False
    gap: bool = False
    clock: bool = False

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
       dated near the clock of this node (else ``clock`` is True).
    2. The movements are consecutive, each one hangs from the hash of the one before and
       adds to the balance, and none is dated after the statement. A movement can be
       dated before the one before it, or before the mark. That is not a fault, because
       the clock of an honest peer can step back. Its date in ``rows`` is the date before
       it, so a window or a price never uses a date that goes back.
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
        return Chain(None, f"the statement is dated {statement.ts}, and it is {int(now)}",
                     CLOCK_FAULT_SEQ, clock=True)
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
    return Chain(_dates_forward(rows, mark), None, None, more=more, gap=gap)


def _dates_forward(rows: List[Row], mark: Mark) -> List[Row]:
    """``rows`` with each date at least the date before it, and the date of the mark."""
    out, last = [], mark.ts if mark.seq is not None else None
    for row in rows:
        if last is not None and row.ts < last:
            row = replace(row, ts=last)
        out.append(row)
        last = row.ts
    return out


def mark_after(chain: Chain, statement: celaut_pb2.BalanceStatement, mark: Mark) -> Mark:
    """The mark once ``chain`` was accepted: at its last movement, or still at ``mark``.
    Its date does not go back."""
    if chain.rows:
        last = chain.rows[-1]
        return Mark(last.seq, last.hash, last.balance_after,
                    max(statement.ts, last.ts) if chain.reaches_head else last.ts)
    if mark.seq is not None and not chain.more:
        return Mark(mark.seq, mark.hash, mark.balance,
                    statement.ts if mark.ts is None else max(statement.ts, mark.ts))
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


def peer_call_timeout() -> float:
    """How long this node waits for a peer to answer ``ModifyDeposit``,
    ``GetBalanceStatement`` or ``GetMetrics``. Without a limit, a peer that does not
    answer stops the thread that asked."""
    try:
        return float(env_manager.get("communication.SIMPLE_RPC_TIMEOUT_SECONDS", 60) or 60)
    except (TypeError, ValueError):
        return 60.0


def top_up_grace_seconds() -> int:
    """How long after this node asked for a top-up its movement may be dated."""
    iteration = int(env_manager.get("MANAGER_ITERATION_TIME", 10) or 10)
    return int(peer_call_timeout()) + 2 * iteration


@dataclass(frozen=True)
class StartAttempt:
    """A start that this node asked the peer for, and that is not closed: no
    ``child_bound`` named the child of its movement yet. ``sent_mu`` is the most that it
    may take, in the MU of the peer. ``ref`` is the child once the peer answered, else "".
    ``start_seq`` is the ``child_start`` that the attempt holds (None: none yet), with
    its amount ``start_mu`` and its date ``start_ts``. ``refused_at`` is when the peer
    refused the start."""
    id: int
    ts: int
    sent_mu: int
    ref: str = ""
    start_seq: Optional[int] = None
    start_mu: int = 0
    start_ts: int = 0
    refused_at: Optional[int] = None


@dataclass(frozen=True)
class TopUpReservation:
    """A top-up that this node sent to the child ``ref``, at ``ts`` on our clock.
    ``mu`` is the part of it that no movement took yet, in the MU of the peer."""
    id: int
    ref: str
    ts: int
    mu: int


@dataclass(frozen=True)
class AccountCheck:
    """What ``account_faults`` found in the movements of our account.

    ``faults`` are the problems, each with the seq where it shows. ``attempts`` is the
    new state of each start attempt that changed, by id: ``(start_seq, start_mu,
    start_ts, closed)``. ``top_ups_taken`` is what the peer took from each top-up
    reservation, by id. Both are saved with the mark, so that a movement is accounted
    for once.
    """
    faults: List[Tuple[int, str]]
    attempts: Dict[int, tuple] = field(default_factory=dict)
    top_ups_taken: Dict[int, int] = field(default_factory=dict)


class _Held:
    """The state of a start attempt while ``account_faults`` reads the movements."""

    def __init__(self, attempt: StartAttempt):
        self.attempt = attempt
        self.seq, self.mu, self.ts = attempt.start_seq, attempt.start_mu, attempt.start_ts
        self.closed = False
        self.changed = False

    @property
    def state(self) -> tuple:
        return self.seq, self.mu, self.ts, self.closed


def account_faults(rows: Iterable[Row], starts: Mapping[str, int],
                   attempts: Iterable[StartAttempt],
                   reservations: Iterable[TopUpReservation],
                   now: float, started: Iterable[str] = (),
                   reaches_head: bool = False) -> AccountCheck:
    """What the peer took from our account that this node did not cause.

    A start is written down before the peer is asked (``attempts``), and a top-up too
    (``reservations``). Every movement that takes MU from our account must match one
    of them, or be the charge of a statement:

    * Each ``child_start`` that is not given back holds one attempt. It is dated at
      most ``start_grace_seconds`` after the attempt, and it takes no more than the
      attempt sent. When no ``child_bound`` names its child yet, it holds the free
      attempt that sent the least of those that pay for it.
    * A ``refund`` of a ``child_start`` sets its attempt free again. The peer charges a
      start for each candidate that it tries, and gives back the start of each one that
      fails.
    * A ``child_bound`` closes the attempt. When it names a child of ours (``starts``),
      the movement goes to the attempt of that child. The movement that held that
      attempt goes to the attempt that the first one held. A child is paid once:
      ``started`` are the children whose start an earlier read showed. When it names a
      child that this node does not know, the attempt is one that never had an answer.
    * An attempt that the peer refused holds no new movement. When the movement that it
      held is not given back by the head of a read after the refusal, that is a fault.
    * A ``top_up`` takes from the reservations of its child that are dated near it.

    The windows are on the clock of this node, never the date of the statement: a peer
    sets that. ``reaches_head``: these movements go up to the head of the account.
    """
    rows = list(rows)
    faults: List[Tuple[int, str]] = []
    refunded, bound = set(), {}
    starts_here = {row.seq for row in rows if row.kind == ledger.CHILD_START and row.amount < 0}
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

    held = {a.id: _Held(a) for a in attempts}
    holder = {h.seq: h for h in held.values() if h.seq is not None}
    of_child = {h.attempt.ref: h for h in held.values() if h.attempt.ref}
    seen = set(started)
    window = start_grace_seconds() + CLOCK_SKEW_SECONDS

    def free(h: _Held, mu: int, ts: int, ref_less: bool) -> bool:
        a = h.attempt
        return (not h.closed and h.seq is None and a.refused_at is None
                and (not ref_less or not a.ref)
                and a.ts - CLOCK_SKEW_SECONDS <= ts <= a.ts + window and mu <= a.sent_mu)

    def hold(h: _Held, seq: int, mu: int, ts: int) -> None:
        h.seq, h.mu, h.ts, h.changed = seq, mu, ts, True
        holder[seq] = h

    def release(h: _Held) -> None:
        holder.pop(h.seq, None)
        h.seq, h.mu, h.ts, h.changed = None, 0, 0, True

    def place(seq: int, mu: int, ts: int, ref_less: bool = False) -> Optional[_Held]:
        fits = [h for h in held.values() if free(h, mu, ts, ref_less)]
        if not fits:
            return None
        best = min(fits, key=lambda h: (h.attempt.sent_mu, h.attempt.ts, h.attempt.id))
        hold(best, seq, mu, ts)
        return best

    def not_asked(seq: int, mu: int, child: str) -> None:
        faults.append((seq, f"movement {seq} pays the start of a child that this node did "
                            f"not ask for ({child[:8] or 'no child'}), {mu} MU"))

    def close(h: _Held) -> None:
        h.closed, h.changed = True, True

    def bind(seq: int, mu: int, ts: int, child: str, at: int) -> None:
        """The movement ``seq`` pays the start of ``child``, as the row ``at`` says."""
        previous = holder.get(seq)
        if child not in starts:
            # A child that this node does not know: the start timed out here, and this
            # node never had its token. Only an attempt with no answer may hold it.
            if previous is not None and not previous.attempt.ref:
                close(previous)
                return
            if previous is not None:
                release(previous)
            placed = place(seq, mu, ts, ref_less=True)
            if placed is None:
                not_asked(at, mu, child)
            else:
                close(placed)
            return
        target = of_child.get(child)
        if target is None or target.closed:
            if child in seen or target is not None:
                if previous is not None:
                    release(previous)
                faults.append((at, f"the start of {child[:8]} was paid more than once"))
                return
            # A child of ours with no attempt here (it was swept): what was sent for it
            # is the bound.
            if previous is not None:
                release(previous)
            seen.add(child)
            if mu > starts[child]:
                faults.append((at, f"the start of {child[:8]} took {mu} MU, and "
                                   f"{starts[child]} were sent"))
            return
        if previous is not target:
            displaced = (target.seq, target.mu, target.ts) if target.seq is not None else None
            if displaced:
                release(target)
            if previous is not None:
                release(previous)
            hold(target, seq, mu, ts)
            if displaced:
                # Two starts at the same time: each movement goes to its own attempt.
                if previous is not None and free(previous, displaced[1], displaced[2], False):
                    hold(previous, *displaced)
                elif place(*displaced) is None:
                    not_asked(displaced[0], displaced[1], "")
        close(target)
        seen.add(child)
        if mu > target.attempt.sent_mu:
            faults.append((at, f"the start of {child[:8]} took {mu} MU, and "
                               f"{target.attempt.sent_mu} were sent"))

    left = {r.id: r.mu for r in reservations}
    reservations = list(reservations)
    top_up_window = top_up_grace_seconds() + CLOCK_SKEW_SECONDS
    taken: Dict[int, int] = {}
    for row in rows:
        if row.kind == ledger.REFUND and row.amount > 0:
            try:
                given_back = holder.get(int(row.detail["refunds_seq"]))
            except (KeyError, TypeError, ValueError):
                given_back = None
            if given_back is not None:
                release(given_back)
            continue
        if row.kind == ledger.CHILD_BOUND:
            try:
                starts_seq = int(row.detail["starts_seq"])
            except (KeyError, TypeError, ValueError):
                continue
            if starts_seq not in starts_here and starts_seq in holder:
                h = holder[starts_seq]
                bind(starts_seq, h.mu, h.ts, row.ref, row.seq)
            continue
        if row.amount >= 0 or row.kind == ledger.STATEMENT_QUERY:
            continue
        if row.kind == ledger.CHILD_START:
            if row.seq in refunded:
                continue
            if row.seq in bound:
                bind(row.seq, -row.amount, row.ts, bound[row.seq], row.seq)
            elif place(row.seq, -row.amount, row.ts) is None:
                not_asked(row.seq, -row.amount, "")
        elif row.kind == ledger.TOP_UP:
            if row.ref not in starts:
                faults.append((row.seq, f"movement {row.seq} tops up an instance that is not ours"))
                continue
            need = -row.amount
            for r in reservations:
                if need <= 0:
                    break
                if r.ref != row.ref or left[r.id] <= 0 or \
                        not r.ts - CLOCK_SKEW_SECONDS <= row.ts <= r.ts + top_up_window:
                    continue
                part = min(need, left[r.id])
                left[r.id] -= part
                taken[r.id] = taken.get(r.id, 0) + part
                need -= part
            if need > 0:
                faults.append((
                    row.seq,
                    f"movement {row.seq} tops up {row.ref[:8]} by {-row.amount} MU, and "
                    f"{-row.amount - need} were sent near its date"))
        else:
            faults.append((
                row.seq, f"movement {row.seq} is a {row.kind!r} charge that this node did not cause"
            ))

    if reaches_head:
        for h in held.values():
            refused_at = h.attempt.refused_at
            if refused_at is not None and h.seq is not None and not h.closed and now >= refused_at:
                faults.append((h.seq, f"the start of movement {h.seq} was refused and not "
                                      f"given back"))
                close(h)
    return AccountCheck(faults, {h.attempt.id: h.state for h in held.values() if h.changed},
                        taken)


def stale_mark(mark_ts: Optional[int], now: float) -> bool:
    """Whether the mark of an account is old enough to read it again, when there is no
    child to read it for: a tenth of the retention that a peer is held to, so that the
    mark is never old enough to excuse movements that a peer hides (``verify_chain``)."""
    return mark_ts is None or now - mark_ts >= ledger.min_peer_retention_days() * 86400 / 10


def refusal_age_seconds() -> float:
    """The age of a mark at which a transport failure can be a refusal: half of the
    retention that a peer is held to. This applies to a peer that answers other calls.
    A gap is excused only after a mark older than that retention (``verify_chain``)."""
    return ledger.min_peer_retention_days() * 86400 / 2


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


def signed_floor(mark_balance: int, free_attempts_mu: int, reserved_mu: int,
                 counters: Mapping[str, int], rates: Mapping[str, int]) -> int:
    """The least that our account on a peer holds now, from what the peer signed.

    The balance of the mark is signed by the peer. After the mark, only what this node
    caused may take MU from the account. That is three things:

    * the starts that it asked for and that hold no movement yet (``free_attempts_mu``);
    * the top-ups that it reserved (``reserved_mu``);
    * the statements that it asked for and whose charge did not show yet.

    A statement costs at most what it was counted for, and at most a full page.
    """
    from src.utils.cost_functions.general_cost_functions import (
        RATE_STATEMENT_QUERY,
        RATE_STATEMENT_ROW,
    )

    query = int(rates.get(RATE_STATEMENT_QUERY, 0))
    row = int(rates.get(RATE_STATEMENT_ROW, 0))
    allowed = counters["reads"] * query + counters["rows_priced"] * row
    most_per_read = query + row * (ledger.max_statement_rows() + 1)
    unshown = min(allowed - counters["verified_mu"],
                  (counters["reads"] - counters["verified"]) * most_per_read)
    return int(mark_balance) - int(free_attempts_mu) - int(reserved_mu) - max(0, unshown)


def floor_on(peer_id: str, books) -> int:
    """``signed_floor`` of our account on ``peer_id``, from ``books`` (a
    ``SQLConnection``). It is 0 with no mark, or when the books cannot be read. The
    check then has only the balance that ``Metrics`` gives."""
    try:
        stored = books.get_peer_statement_mark(peer_id)
        if not stored:
            return 0
        # A movement after the mark is dated at the mark or later (``verify_chain``), so
        # an attempt or a reservation whose window ended before the mark takes nothing.
        since = int(stored.get("ts") or 0) - CLOCK_SKEW_SECONDS
        start_window = start_grace_seconds() + CLOCK_SKEW_SECONDS
        top_up_window = top_up_grace_seconds() + CLOCK_SKEW_SECONDS
        attempts = [StartAttempt(*a) for a in books.open_start_attempts(peer_id)]
        free_mu = sum(a.sent_mu for a in attempts if a.start_seq is None and a.refused_at is None
                      and a.ts + start_window >= since)
        reserved = sum(int(r[3]) for r in books.top_up_reservations(peer_id)
                       if int(r[2]) + top_up_window >= since)
        return max(0, signed_floor(int(stored.get("balance_mu") or 0), free_mu, reserved,
                                   books.get_statement_counters(peer_id), stored_rates(peer_id)))
    except Exception as e:
        log.LOGGER(f"Cannot read the signed balance of our account on {peer_id}: {e}")
        return 0


def read(peer_id: str, client_id: str, *, token: str = "", from_seq: int,
         rows: Optional[int] = None) -> celaut_pb2.BalanceStatement:
    """``balance_statement.fetch``, with the books of what the peer may charge for it.

    ``rows`` is how many movements the read asks for (None: the node decides). Our
    account pays for every read, and the peer writes each charge in it, so each read is
    counted when it is asked, before the peer can charge it: a read that fails after the
    charge still has its row. The rows that it may be priced for are the one of its own
    charge, and then those of the answer. A read that fails is written down with its
    range and time, and ``settle_failed_reads`` prices it later. ``from_seq`` 0 asks for
    the head only: no movement. The read waits ``peer_call_timeout`` at most.
    """
    from src.database.sql_connection import SQLConnection
    from src.manager import balance_statement

    sc = SQLConnection()
    sc.add_statement_read(peer_id, rows_priced=1)
    asked = rows or ledger.max_statement_rows()
    to_seq = from_seq + rows - 1 if rows else 0
    try:
        statement = balance_statement.fetch(
            peer_id, client_id, token=token, from_seq=from_seq, to_seq=to_seq,
            timeout=peer_call_timeout())
    except Exception:
        if from_seq > 0:
            sc.add_failed_statement_read(
                peer_id, ledger.account_ref(token or client_id), from_seq,
                from_seq + asked - 1, asked, own=not token)
        raise
    sc.add_statement_read(peer_id, rows_priced=len(statement.movements), count=0)
    return statement


def _detail_int(detail: Mapping, key: str) -> Optional[int]:
    try:
        return int(detail[key])
    except (KeyError, TypeError, ValueError):
        return None


def priced_own_reads(pending: Iterable[tuple], rows: Iterable[Row], now: float,
                     reaches_head: bool) -> Dict[int, int]:
    """The failed reads of our own account (``(id, from_seq, to_seq, ts, ...)``) that
    ``rows`` settle, with the rows that each one may be priced for.

    The peer writes the charge of a read in the same transaction as the read. The
    charge is a ``statement_query`` movement that names the range of the read. So the
    rows that existed for the read are those of its range before its charge. That is by seq, and
    a date that the peer writes changes nothing. A read whose charge does not show by
    the head of the account, long after it was asked, was not charged: it counts for 0.
    """
    rows = list(rows)
    charges = [row for row in rows
               if row.kind == ledger.STATEMENT_QUERY and row.amount < 0 and not row.ref]
    used, priced = set(), {}
    for read_id, from_seq, to_seq, ts, *_ in pending:
        charge = next((row for row in charges if row.seq not in used and row.seq >= from_seq
                       and _detail_int(row.detail, "from_seq") == from_seq), None)
        if charge is not None:
            used.add(charge.seq)
            priced[read_id] = max(0, min(to_seq, charge.seq - 1) - from_seq + 1)
        elif reaches_head and now > ts + peer_call_timeout() + CLOCK_SKEW_SECONDS:
            priced[read_id] = 0
    return priced


def priced_child_reads(pending: Iterable[tuple], rows: Iterable[Row], head_seq: int
                       ) -> Tuple[Dict[int, int], Dict[int, Tuple[int, int]]]:
    """The failed reads of a child (``(id, from_seq, to_seq, ts, seen_to, rows_seen)``)
    whose range ``rows`` show. ``rows`` are the movements of the child that were just
    checked up to ``head_seq``.

    A read counts for the rows of its range that are dated before it. The statements
    can show the range over several pages. Each page adds the rows that it shows. The
    read is settled when a page gets to the end of the range or to the head. Returns
    the settled reads with their rows, and the progress of the others.
    """
    rows = list(rows)
    priced, progress = {}, {}
    for read_id, from_seq, to_seq, ts, seen_to, rows_seen in pending:
        if from_seq > head_seq:
            priced[read_id] = 0
            continue
        first = from_seq if seen_to is None else seen_to + 1
        last = min(to_seq, head_seq)
        shown = [row for row in rows if first <= row.seq <= last]
        if not shown or shown[0].seq != first:
            continue
        count = rows_seen + sum(1 for row in shown if row.ts <= ts + CLOCK_SKEW_SECONDS)
        if shown[-1].seq >= last:
            priced[read_id] = count
        else:
            progress[read_id] = (shown[-1].seq, count)
    return priced, progress


def settle_failed_reads(peer_id: str, account: str, rows: Iterable[Row], head_seq: int, *,
                        own: bool = False, now: Optional[float] = None,
                        reaches_head: bool = False) -> None:
    """Price the failed reads of ``account`` that the checked movements settle:
    ``priced_own_reads`` for our own account (``own``), else ``priced_child_reads``."""
    from src.database.sql_connection import SQLConnection

    sc = SQLConnection()
    pending = sc.failed_statement_reads(peer_id, ledger.account_ref(account))
    if not pending:
        return
    if own:
        priced, progress = priced_own_reads(
            pending, rows, time.time() if now is None else now, reaches_head), {}
    else:
        priced, progress = priced_child_reads(pending, rows, head_seq)
    if priced or progress:
        sc.settle_failed_statement_reads(peer_id, priced, progress)


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


def still_pays(peer_id: str, rows_asked: int, floor: int = 0) -> bool:
    """Whether our balance on ``peer_id`` still pays for a read of ``rows_asked``
    movements. The balance is the one that ``Metrics`` gives now, but never less than
    ``floor`` (``signed_floor``): a peer cannot say a lower balance than it signed."""
    from src.manager.metrics import refresh_balance_on_peer

    try:
        balance = refresh_balance_on_peer(peer_id)
    except Exception:
        balance = 0
    return page_rows(max(balance, floor), stored_rates(peer_id)) >= rows_asked


def penalise_no_statement(peer_id: str, account: str, error: Exception, seq: int = 0,
                          child: bool = False, pays=None, answered: bool = False,
                          mark_ts: Optional[int] = None) -> None:
    """A peer that gives no valid statement is one whose statement does not add up.

    Not when ``child`` and the peer answers ``UNIMPLEMENTED``: it may have delegated the
    child on, and the proto lets a node say so (the child is then charged from its
    balance, as with no statement). ``RESOURCE_EXHAUSTED`` is a refusal like any other
    when ``pays()`` says that our balance still pays for what was asked (``page_rows``):
    a peer that says we cannot pay for it contradicts the balance that it reports or
    signed. When the balance fell in between, it is not a fault. ``seq`` is the
    movement where the statement was missing.

    A transport failure (``unreachable``) says nothing about the statement, as a rule.
    But the peer selects the code that it answers with. The failure is a refusal when
    two conditions are true. The peer ``answered`` another call in the same tick. The
    mark (dated ``mark_ts``) is older than ``refusal_age_seconds``. If not, the mark
    grows old enough to excuse movements that the peer hides. With no date, the failure
    is not a refusal.
    """
    code = _code_of(error)
    if child and code == grpc.StatusCode.UNIMPLEMENTED:
        return
    if unreachable(error):
        if not answered or mark_ts is None or time.time() - int(mark_ts) < refusal_age_seconds():
            return
    elif code == grpc.StatusCode.RESOURCE_EXHAUSTED and pays is not None and not pays():
        return
    # A refusal shows no movement, and it would be the same fault at every read for as
    # long as the peer keeps refusing: it is scored once in a window instead.
    penalise_once(peer_id, account, Reason.STATEMENT_INCONSISTENT,
                  f"no valid statement: {error}", seq, window=FAULT_WINDOW_SECONDS)


def penalise_chain(peer_id: str, account: str, chain: Chain, seq: int) -> None:
    """Score a statement that ``verify_chain`` did not accept, at its ``fault_seq``, or
    at ``seq``. A statement dated far from our clock shows no movement, and the clock
    that is wrong can be ours. It is scored once in a window, not at each read."""
    if chain.clock:
        penalise_once(peer_id, account, Reason.STATEMENT_INCONSISTENT, chain.problem,
                      CLOCK_FAULT_SEQ, window=FAULT_WINDOW_SECONDS)
    else:
        penalise_once(peer_id, account, Reason.STATEMENT_INCONSISTENT, chain.problem,
                      chain.fault_seq if chain.fault_seq is not None else seq)


def _invalid_statement():
    from src.manager.balance_statement import StatementInvalid
    return StatementInvalid


def penalise_once(peer_id: str, account: str, reason: str, problem: str, seq: int = 0,
                  window: int = 0) -> None:
    """Score a fault of a peer, once: the same fault read again costs it nothing.

    A fault is the peer, the account (by ``account_ref``, never the credential), the
    reason and the seq of the movement where it shows. A statement that does not add up
    stays wrong for every read until the mark goes past it, and also after a restart,
    so the record is in the database. A different fault, at another movement, is
    scored. A fault that shows no movement has a ``window`` (seconds): it is scored
    again only when the last one is older than that (``statement_faults``).
    """
    from src.database.sql_connection import SQLConnection
    from src.reputation_system import interface

    account_ref = ledger.account_ref(account)
    log.LOGGER(f"Statement of {account_ref[:8]} on {peer_id}: {problem}")
    if not SQLConnection().record_statement_fault(peer_id, account_ref, reason, seq, window):
        return
    interface.update_peer_reputation(peer_id=peer_id, amount=STATEMENT_PENALTY, reason=reason)

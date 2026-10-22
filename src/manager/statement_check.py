"""Checking what a peer charges, from its signed statements (#570).

A balance cannot tell a wrong charge from a legitimate one: it is the sum of every
movement, and many movements move it. A statement can. This node reads the statement
of its own account on a peer and of each child that it delegated there, and checks
two things:

1. It adds up. From the last movement that this node checked (the *mark*), each new
   movement takes the balance to its ``balance_after``, the seqs have no gap, and the
   last one is the balance of the answer.
2. Each movement follows its rule. On a child: the start is what this node sent, the
   build is within the quote, a maintenance period is within the rates and the
   resources that it names, and the periods do not add up to more time than passed.
   On our account: nothing takes MU from it that this node did not cause.

A peer whose statement does not add up, or shows that it took more than its rule
allows, loses ``STATEMENT_PENALTY``: the same as a service that does not hash to its
id, because in every case the peer signed something false about money.

The functions here are pure; ``maintain`` and ``delegate_execution`` read the
statements and act on what these return.
"""
import json
import math
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

import grpc

from protos import celaut_pb2
from src.database import ledger
from src.reputation_system.reasons import Reason
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.cost_functions.general_cost_functions import (
    RATE_CPU_PER_VCPU_SECOND,
    RATE_DISK_PER_GIB_SECOND,
    RATE_RAM_PER_GIB_SECOND,
    RATE_RAM_PER_GIB_SECOND_ARCH_PREFIX,
    RATE_SCARCITY_MAX_MULTIPLIER,
)
from src.utils.monetary import GIB
from src.utils.utils import from_amount

env_manager = ConfigManager()

STATEMENT_PENALTY = -1000

# Room for the clocks: a ts is whole seconds, and a tick may come a moment early.
CLOCK_SLACK_SECONDS = 2

# The span that a read asks for is the time since the last read, plus this. A movement
# written just before the last read reached the peer is then still in the next answer.
SINCE_MARGIN_SECONDS = 60

# The factor k of EstimatedCost.variance: a build may cost (1 + variance * k) times its
# quote.
VARIANCE_FACTOR = 1


@dataclass(frozen=True)
class Row:
    seq: int
    ts: int
    amount: int
    balance_after: int
    kind: str
    ref: str
    detail: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class Mark:
    """The last movement that this node checked. ``seq`` None: none yet."""
    seq: Optional[int]
    balance: int
    ts: Optional[int] = None


def tick_detail(sys_req, seconds: int) -> dict:
    """The cause of a maintenance period: what a father checks it against."""
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
            ref=m.instance_ref, detail=_detail(m.detail))
        for m in statement.movements
    ]


def new_rows(statement: celaut_pb2.BalanceStatement, mark: Mark
             ) -> Tuple[Optional[List[Row]], Optional[str]]:
    """The movements after ``mark``, if the statement adds up.

    Returns ``(rows, None)`` when it does, ``(None, problem)`` when it does not, and
    ``(None, None)`` when it cannot be checked: the node cut the answer at its limit
    before the movement after the mark. That is a span too long to ask for, not a fault
    of the peer.
    """
    rows = rows_of(statement)
    balance = from_amount(statement.balance)
    for previous, row in zip(rows, rows[1:]):
        if row.seq != previous.seq + 1:
            return None, f"movements {previous.seq + 1} to {row.seq - 1} are missing"
        if previous.balance_after + row.amount != row.balance_after:
            return None, f"movement {row.seq} does not add up"
    if rows and rows[0].seq == 1 and rows[0].amount != rows[0].balance_after:
        return None, "the first movement does not start from 0"
    if rows and rows[-1].balance_after != balance:
        return None, "the balance is not the balance after the last movement"
    if mark.seq is None:
        return rows, None
    if not rows:
        if balance != mark.balance:
            return None, f"the balance moved after movement {mark.seq} with no movement"
        return [], None
    if rows[0].seq <= mark.seq:
        # The mark is in the answer, and it must be the movement that was checked.
        index = mark.seq - rows[0].seq
        if index >= len(rows):
            return None, f"the statement ends before movement {mark.seq}, which it showed"
        if rows[index].balance_after != mark.balance:
            return None, f"movement {mark.seq} is not the movement that it was"
        return rows[index + 1:], None
    if rows[0].seq != mark.seq + 1:
        if statement.truncated:
            return None, None
        return None, f"movements {mark.seq + 1} to {rows[0].seq - 1} are missing"
    if mark.balance + rows[0].amount != rows[0].balance_after:
        return None, f"movement {rows[0].seq} does not add up from movement {mark.seq}"
    return rows, None


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


def maintenance_bound(detail: Mapping, rates: Mapping[str, int]) -> Optional[int]:
    """The most that a maintenance period with this ``detail`` can cost on the peer.

    The resources and the seconds come from the period itself; the prices from the
    rates that the peer announces, at their highest multiplier. Each per-second rate
    gets 1 MU more, because the peer announces its hourly price divided and truncated.
    None when the period does not name its resources: a peer that delegated the
    instance again meters it from its own peer, and cannot.
    """
    try:
        seconds = int(detail["seconds"])
        mem = int(detail["mem_limit"])
        disk = int(detail["disk_space"])
        period = int(detail["cpu_period"])
        quota = int(detail["cpu_quota"])
    except (KeyError, TypeError, ValueError):
        return None
    ram_rates = [v for k, v in rates.items()
                 if k == RATE_RAM_PER_GIB_SECOND or k.startswith(RATE_RAM_PER_GIB_SECOND_ARCH_PREFIX)]
    ram = max(ram_rates or [0]) + 1
    cpu = rates.get(RATE_CPU_PER_VCPU_SECOND, 0) + 1
    disk_rate = rates.get(RATE_DISK_PER_GIB_SECOND, 0) + 1
    multiplier = max(1, rates.get(RATE_SCARCITY_MAX_MULTIPLIER, 1))
    vcpus = quota / period if period > 0 else 0
    per_second = ram * mem / GIB + cpu * vcpus + disk_rate * disk / GIB
    return math.ceil(per_second * max(0, seconds) * multiplier)


def build_bound(build_peer_mu: int, variance: float) -> int:
    """The most that the start of a child can take for its build, in the MU of the peer."""
    return math.ceil(int(build_peer_mu) * (1 + max(0.0, float(variance)) * VARIANCE_FACTOR))


# What a child does on its own, with its own balance: spend on its own children,
# tunnels, a resize, its statements; or get MU back from them. None of it is a charge
# of the peer on this node.
_CHILD_ACTIVITY = (
    ledger.CHILD_START, ledger.CHILD_BOUND, ledger.TOP_UP, ledger.WITHDRAWAL, ledger.REFUND, ledger.TUNNEL,
    ledger.RESOURCE_CHANGE, ledger.STATEMENT_QUERY, ledger.DEPOSIT, ledger.CREDIT,
)


def child_problems(rows: Iterable[Row], *, mark: Mark, rates: Mapping[str, int],
                   sent_peer_mu: int = 0, start_bound_peer_mu: Optional[int] = None
                   ) -> List[str]:
    """What the peer took from a child beyond its rule.

    ``start_bound_peer_mu`` is the most that the start may take for the build and the
    commission of the peer: given at the start, None at a tick, where a start cost is
    not expected at all.
    """
    rows = list(rows)
    problems = []
    start_cost = 0
    for row in rows:
        if row.kind == ledger.START:
            if row.seq != 1:
                problems.append(f"a second start at movement {row.seq}")
            elif row.amount < sent_peer_mu:
                problems.append(f"the start got {row.amount} MU, and {sent_peer_mu} were sent")
        elif row.kind in (ledger.BUILD, ledger.COMMISSION):
            start_cost -= row.amount
            if start_bound_peer_mu is None:
                problems.append(f"a {row.kind} after the start, at movement {row.seq}")
        elif row.kind == ledger.MAINTENANCE:
            bound = maintenance_bound(row.detail, rates)
            if bound is not None and -row.amount > bound:
                problems.append(
                    f"maintenance {row.seq} took {-row.amount} MU, more than the "
                    f"{bound} MU that its resources cost at the rates of the peer"
                )
        elif row.kind not in _CHILD_ACTIVITY and row.amount < 0:
            problems.append(f"movement {row.seq} is a {row.kind!r} charge")
    if start_bound_peer_mu is not None and not any(row.kind == ledger.START for row in rows):
        problems.append("the start is not in the statement")
    if start_bound_peer_mu is not None and start_cost > start_bound_peer_mu:
        problems.append(
            f"the start took {start_cost} MU for the build, more than the "
            f"{start_bound_peer_mu} MU of the quote"
        )
    problems.extend(_time_problems(rows, mark))
    return problems


def _time_problems(rows: List[Row], mark: Mark) -> List[str]:
    """Maintenance periods that add up to more time than passed since the mark."""
    periods = [row for row in rows if row.kind == ledger.MAINTENANCE]
    if mark.ts is None or not periods:
        return []
    seconds = []
    for row in periods:
        try:
            seconds.append(int(row.detail["seconds"]))
        except (KeyError, TypeError, ValueError):
            return []
    elapsed = rows[-1].ts - mark.ts
    # One period of room: the first one may be charged right after the mark.
    if sum(seconds) > elapsed + max(seconds) + CLOCK_SLACK_SECONDS:
        return [f"{len(periods)} maintenance periods charge {sum(seconds)} s in {elapsed} s"]
    return []


def caused_by_father(row: Row) -> bool:
    """The child's side of a ModifyDeposit of its father: not consumption."""
    return (row.kind == ledger.TOP_UP and row.amount > 0) or \
        (row.kind == ledger.WITHDRAWAL and row.amount < 0)


def consumption(rows: Iterable[Row]) -> int:
    """What the child spent in ``rows``, in the MU of the peer: every movement but
    those of its father. Never below 0."""
    return max(0, -sum(row.amount for row in rows if not caused_by_father(row)))


def start_grace_seconds() -> int:
    """How long a ``child_start`` may wait for its token, or for its refund."""
    timeout = int(env_manager.get("START_SERVICE_ON_PEER_TIMEOUT", 0) or 0)
    iteration = int(env_manager.get("MANAGER_ITERATION_TIME", 10) or 10)
    return (timeout if timeout > 0 else 600) + 2 * iteration


def account_problems(rows: Iterable[Row], starts: Mapping[str, int], now_ts: int
                     ) -> Tuple[List[str], Optional[int]]:
    """What the peer took from our account that this node did not cause.

    ``starts`` is each child that this node started on the peer, by the ``instance_ref``
    of its token on the peer, with what it sent for it. The peer names the child of a
    ``child_start`` in a later ``child_bound`` row. Returns the problems, and the seq of
    the first ``child_start`` that cannot be judged yet: one with no ``child_bound``, or
    with a child that this node has not stored, younger than ``start_grace_seconds``.
    That start may be in progress. The mark stops before it, and the next read looks at
    it again.
    """
    rows = list(rows)
    problems = []
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
    for row in rows:
        if row.amount >= 0 or row.kind == ledger.STATEMENT_QUERY:
            continue
        if row.kind == ledger.CHILD_START:
            if row.seq in refunded:
                continue
            child = bound.get(row.seq, "")
            if child in starts:
                if -row.amount > starts[child]:
                    problems.append(
                        f"the start of {child[:8]} took {-row.amount} MU, and "
                        f"{starts[child]} were sent"
                    )
                continue
            if now_ts - row.ts <= start_grace_seconds():
                return problems, row.seq
            problems.append(f"movement {row.seq} pays the start of a child that this node did not start")
        elif row.kind == ledger.TOP_UP:
            if row.ref not in starts:
                problems.append(f"movement {row.seq} tops up an instance that is not ours")
        else:
            problems.append(f"movement {row.seq} is a {row.kind!r} charge that this node did not cause")
    return problems, None


def since_seconds(read_at: Optional[float], now: Optional[float] = None) -> int:
    """The span to ask for: since the last read, with a margin. 0 (no span) for none."""
    if not read_at:
        return 0
    now = time.time() if now is None else now
    return max(1, math.ceil(now - float(read_at))) + SINCE_MARGIN_SECONDS


def unreachable(error: Exception) -> bool:
    """A transport failure, which says nothing about the statement of the peer."""
    return isinstance(error, grpc.RpcError) and error.code() in (
        grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED,
    )


def _short_of_statement_price(peer_id: str) -> bool:
    """Whether our account on ``peer_id`` may not pay for a statement now.

    A peer refuses a statement that the account cannot pay, and that is not its fault.
    The price is read from its rates, for a few rows; a balance that cannot be read
    counts as short, so that a doubt never costs the peer the penalty.
    """
    from src.manager.metrics import refresh_balance_on_peer
    from src.utils.cost_functions.general_cost_functions import (
        RATE_STATEMENT_QUERY,
        RATE_STATEMENT_ROW,
    )

    rates = stored_rates(peer_id)
    price = rates.get(RATE_STATEMENT_QUERY, 0) + 10 * rates.get(RATE_STATEMENT_ROW, 0)
    try:
        return refresh_balance_on_peer(peer_id) < price
    except Exception:
        return True


def penalise_no_statement(peer_id: str, account: str, error: Exception) -> None:
    """A peer that gives no valid statement is one whose statement does not add up.

    Not when the peer cannot be reached (that says nothing about its statements), nor
    when our account there cannot pay for one.
    """
    if unreachable(error):
        return
    if not isinstance(error, _invalid_statement()) and _short_of_statement_price(peer_id):
        log.LOGGER(f"No statement of {account[:8]} from {peer_id}, and our account there "
                   f"may not pay for one: {error}")
        return
    penalise_once(peer_id, account, Reason.STATEMENT_INCONSISTENT, f"no valid statement: {error}")


def _invalid_statement():
    from src.manager.balance_statement import StatementInvalid
    return StatementInvalid


# (peer, account, reason) already scored. A statement that does not add up stays wrong
# for every later read until the mark moves past it, and one fault must cost the peer
# one penalty, not one each tick. In memory: a restart costs at most one more.
_penalised = set()


def penalise_once(peer_id: str, account: str, reason: str, problem: str) -> None:
    from src.reputation_system import interface

    log.LOGGER(f"Statement of {account[:8]} on {peer_id}: {problem}")
    if (peer_id, account, reason) in _penalised:
        return
    _penalised.add((peer_id, account, reason))
    interface.update_peer_reputation(peer_id=peer_id, amount=STATEMENT_PENALTY, reason=reason)

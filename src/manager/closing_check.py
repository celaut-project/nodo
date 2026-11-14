"""Judge a peer that changed a delegated instance in a way its quote did not say (issue #344, 7).

The delegating node keeps what the quote of the peer said about closing time on the row of
the instance (`quote_on_close`, `quote_closes_at`). The quote is the reference. The
statement of the child, signed by the peer, shows what the peer did:

* A **stop**: the `stop` row with `detail.reason` `activity_window`. Other reasons are not
  a decision of the peer about its hours: `process_lost` and `guest_panic` are failures,
  `out_of_balance` is the balance, and `father_stop` and `operator_kill` can be the stop that this node or its
  client asked for.
* A **hibernation**: a `maintenance` row that charges no memory (`detail.mem_limit` 0)
  after one that charged memory, with no `resource_change` row between them. A
  `resource_change` row is what a resize that the instance asked for leaves, so a
  hibernation that has none was not asked for by the instance.

What the quote said, and what is scored:

| The quote said | Hibernation | Stop |
|---|---|---|
| `REFUSE`, or `closes_at` 0 | before or after: unannounced | unannounced |
| `HIBERNATE`, close at T | before T: unannounced; from T: expected | unannounced at any time |
| `STOP`, close at T | before T: unannounced; from T: milder than the quote, no score | before T: unannounced; from T: expected |

A stop costs more than a hibernation: a hibernated instance keeps its disk, and a stopped
one is destroyed. The weights are chosen here, with `INSTANCE_LOST` (-100) as the
reference. A row with no quote (`quote_on_close` None) is not judged. An instance that ran
out of balance scores nothing: `INSTANCE_OUT_OF_BALANCE` is retired (#562).

This judgement needs the statement to be read. A peer that stopped the child and that no
longer answers the statement of a dead account (the retention of the peer deleted it) is
not scored: the limit is in `docs/REPUTATION.md`.
"""
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional

from src.database import ledger
from src.manager import statement_check
from src.reputation_system.reasons import Reason
from src.utils import dead_instances

HIBERNATED_UNANNOUNCED_PENALTY = -50
STOPPED_UNANNOUNCED_PENALTY = -200

HIBERNATE = "hibernate"
STOP = "stop"


@dataclass(frozen=True)
class Fault:
    reason: str
    amount: int
    seq: int
    problem: str


def _period_start(row) -> int:
    return int(row.ts) - max(0, _int_of(row.detail.get("seconds")))


def _int_of(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _no_memory(row) -> bool:
    return str(row.detail.get("mem_limit", "")) == "0"


def hibernations(rows: Iterable, previous_mem: Optional[int]) -> List:
    """The ``maintenance`` rows where an instance went from holding memory to none, with
    no ``resource_change`` row since the last one that held it.

    ``previous_mem`` is the memory that the instance was charged for before these rows
    (the row of the delegation): None when it is not known, and then the first
    ``maintenance`` row is not taken for a change.
    """
    found = []
    holding = None if previous_mem is None else previous_mem > 0
    asked_for_resize = False
    for row in sorted(rows, key=lambda r: r.seq):
        if row.kind == ledger.RESOURCE_CHANGE:
            asked_for_resize = True
        elif row.kind == ledger.MAINTENANCE and row.amount < 0:
            empty = _no_memory(row)
            if empty and holding and not asked_for_resize:
                found.append(row)
            holding = not empty
            asked_for_resize = False
    return found


def judge(rows, on_close: Optional[str], closes_at: Optional[int],
          previous_mem: Optional[int] = None) -> List[Fault]:
    """The unannounced changes in ``rows`` against what the quote said. [] if none."""
    if on_close is None:
        return []
    on_close = str(on_close)
    closes_at = int(closes_at or 0)
    skew = statement_check.CLOCK_SKEW_SECONDS
    # With no scheduled close there is nothing that the quote announced.
    scheduled = on_close in (HIBERNATE, STOP) and closes_at > 0
    faults: List[Fault] = []

    for row in hibernations(rows, previous_mem):
        start = _period_start(row)
        if scheduled and start >= closes_at - skew:
            # From T: what a `hibernate` quote said, and milder than a `stop` quote.
            continue
        faults.append(Fault(
            Reason.INSTANCE_HIBERNATED_UNANNOUNCED, HIBERNATED_UNANNOUNCED_PENALTY, row.seq,
            f"the peer hibernated the instance (maintenance row {row.seq}) and its quote "
            f"said {on_close!r} with a close at {closes_at}"))

    for row in rows:
        if row.kind != ledger.STOP or str(row.detail.get("reason", "")) != dead_instances.ACTIVITY_WINDOW:
            continue
        if on_close == STOP and scheduled and row.ts >= closes_at - skew:
            continue
        faults.append(Fault(
            Reason.INSTANCE_STOPPED_UNANNOUNCED, STOPPED_UNANNOUNCED_PENALTY, row.seq,
            f"the peer stopped the instance (stop row {row.seq}) and its quote said "
            f"{on_close!r} with a close at {closes_at}"))
    return faults


def check(row: Mapping, rows, statement=None) -> None:
    """Score the peer of the delegated ``row`` for the changes in ``rows`` that its quote
    did not say. Once for each change: the same row read again costs nothing. Never raises.

    ``statement`` is the page of ``rows``: with the page where the peer signed its quote,
    it is the evidence of the fault (#652)."""
    try:
        mem = row.get("mem_limit")
        for fault in judge(rows, row.get("quote_on_close"), row.get("quote_closes_at"),
                           None if mem is None else int(mem)):
            statement_check.penalise_once(
                row.get("peer_id"), row["token"], fault.reason, fault.problem, fault.seq,
                amount=fault.amount,
                evidence=statement_check.quote_evidence(fault.reason, row, statement))
    except Exception as e:
        from src.utils import logger as log
        log.LOGGER(f"Could not check the closing terms of {row.get('id')}: {e}")

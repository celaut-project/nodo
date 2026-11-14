"""Faults that a third party can check from signed messages alone (#652).

A ``FaultEvidence`` holds balance statements and execution receipts that one node, the
*accused*, signed. :func:`check` tells whether they prove the fault that they name. It
needs only the public key of the accused: no database, no mark, no clock of this node and
nothing that only the reporter knows. So a node can believe the evidence of a node that it
does not trust at all, and a reporter attaches evidence only when :func:`check` proves the
fault from it (:func:`attach`).

What each reason needs:

* ``statement_inconsistent``: one statement whose chain does not hold in the page itself
  (``statement_check.verify_chain`` from no mark), or two statements of one account that
  disagree: a seq with two hashes, or a head that went back.
* ``killed_with_mu``: one statement with a ``stop`` row that keeps MU of an orphan
  (:func:`killed_with_mu`, the rule of ``balance_statement._judge_stop``).
* ``statement_contradicts_receipt``: statements of one account and receipts of the
  ``DescendantTunnel`` of that account that do not agree (``receipt_statement.check``).
* ``maintenance_overcharged``, ``instance_hibernated_unannounced``,
  ``instance_stopped_unannounced``: the page with movement 1 of the account, the ``start``
  that holds the quote of the accused (``detail.quote``), and the page with the charges.
  The charges are judged after the first movement of their page: that movement is the
  mark, so no time before it counts.
"""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from protos import celaut_pb2
from src.database import ledger
from src.identity import execution_receipt, statement_signature
from src.manager import closing_check, statement_check
from src.reputation_system.reasons import Reason

# What a fault costs the accused, as when this node sees it itself.
PENALTIES: Dict[str, int] = {
    Reason.STATEMENT_INCONSISTENT: statement_check.STATEMENT_PENALTY,
    Reason.KILLED_WITH_MU: statement_check.STATEMENT_PENALTY,
    Reason.STATEMENT_CONTRADICTS_RECEIPT: statement_check.STATEMENT_PENALTY,
    Reason.MAINTENANCE_OVERCHARGED: statement_check.STATEMENT_PENALTY,
    Reason.INSTANCE_HIBERNATED_UNANNOUNCED: closing_check.HIBERNATED_UNANNOUNCED_PENALTY,
    Reason.INSTANCE_STOPPED_UNANNOUNCED: closing_check.STOPPED_UNANNOUNCED_PENALTY,
}
PROVABLE = frozenset(PENALTIES)

# The most statements and receipts that one evidence may hold. More is refused, not cut.
MAX_STATEMENTS = 4
MAX_RECEIPTS = 16

# The reasons of a ``stop`` row of an instance that could not pay its next tick.
OUT_OF_BALANCE = frozenset(("out_of_balance", "hibernated_out_of_balance"))
# A ``detail.tick`` above this many times the largest maintenance row is not believed.
MAX_TICK_FACTOR = 10


@dataclass(frozen=True)
class Proven:
    """A fault that the evidence proves: the account of the statements, by
    ``account_ref``, and the seq of the movement where the fault shows."""
    reason: str
    accused: str
    account_ref: str
    seq: int
    problem: str

    @property
    def penalty(self) -> int:
        return PENALTIES[self.reason]


def _int_of(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def killed_with_mu(rows: Sequence) -> List[tuple]:
    """The ``stop`` rows in ``rows`` that keep MU of an orphan, as ``(seq, problem)``.

    The rule of #631: a ``stop`` row with ``detail.leftover`` above 0 whose row before it
    is a ``refund`` with ``detail.lost = father_gone``. The reason of the row does not
    matter, but for ``out_of_balance`` with less MU left than the tick that the instance
    could not pay (``detail.tick``, or the largest maintenance row when the row has none,
    and never more than ``MAX_TICK_FACTOR`` times it).
    """
    lost = {row.seq for row in rows
            if row.kind == ledger.REFUND and row.amount < 0
            and row.detail.get("lost") == "father_gone"}
    period = max((-row.amount for row in rows
                  if row.kind == ledger.MAINTENANCE and row.amount < 0), default=0)
    found = []
    for row in rows:
        if row.kind != ledger.STOP or (row.seq - 1) not in lost:
            continue
        leftover = _int_of(row.detail.get("leftover"))
        reason = str(row.detail.get("reason", ""))
        if leftover <= 0:
            continue
        if reason in OUT_OF_BALANCE:
            tick = _int_of(row.detail.get("tick")) if "tick" in row.detail else None
            if tick is None:
                tick = period
            elif period:
                tick = min(tick, period * MAX_TICK_FACTOR)
            if not tick or leftover < tick:
                continue
        found.append((row.seq, f"stop row {row.seq} ({reason}) with {leftover} MU left "
                               f"and the father gone"))
    return found


def _chain_problem(statement: celaut_pb2.BalanceStatement) -> Optional[tuple]:
    """``(seq, problem)`` when the page does not hold together by itself, else None."""
    chain = statement_check.verify_chain(
        statement, statement_check.NO_MARK, now=statement.ts, from_seq=statement.from_seq)
    if chain.rows is None and not chain.clock:
        return chain.fault_seq or 0, chain.problem
    return None


def _hashes(statement: celaut_pb2.BalanceStatement) -> Dict[int, str]:
    """The hash of each seq that the page shows: of its movements, of the head, and of
    the movement before each movement (its ``prev_hash``)."""
    hashes = {}
    for movement in statement.movements:
        if movement.seq > 1:
            hashes.setdefault(int(movement.seq) - 1, movement.prev_hash)
    for row in statement_check.rows_of(statement):
        hashes[row.seq] = row.hash
    if statement.head_seq:
        hashes.setdefault(int(statement.head_seq), statement.head_hash)
    return hashes


def _equivocation(first: celaut_pb2.BalanceStatement,
                  second: celaut_pb2.BalanceStatement) -> Optional[tuple]:
    """``(seq, problem)`` when two pages of one account disagree."""
    a, b = _hashes(first), _hashes(second)
    for seq in sorted(set(a) & set(b)):
        if a[seq] != b[seq]:
            return seq, f"movement {seq} has two hashes: {a[seq][:12]} and {b[seq][:12]}"
    early, late = sorted((first, second), key=lambda s: s.ts)
    if late.ts > early.ts and late.head_seq < early.head_seq:
        return int(late.head_seq), (f"the head went back from movement {early.head_seq} "
                                    f"to {late.head_seq}")
    return None


def _rows(statements: Iterable[celaut_pb2.BalanceStatement]) -> List:
    """The rows of the pages, once each, in the order of their seqs."""
    by_seq = {}
    for statement in statements:
        for row in statement_check.rows_of(statement):
            by_seq.setdefault(row.seq, row)
    return [by_seq[seq] for seq in sorted(by_seq)]


def _quote_and_charges(statements: Sequence[celaut_pb2.BalanceStatement]):
    """``(quote_row, charges)``: the quote of the ``start`` movement as row columns, and
    the rows of the last page after its first movement. None when it cannot be read."""
    rows = _rows(statements)
    start = next((row for row in rows if row.seq == 1), None)
    quote_row = None if start is None else statement_check.signed_quote_row(start)
    if quote_row is None:
        return None
    last = statement_check.rows_of(statements[-1])
    if len(last) < 2:
        return None
    return quote_row, last


def _prove(evidence: celaut_pb2.FaultEvidence, accused: str,
           statements: Sequence[celaut_pb2.BalanceStatement]) -> Optional[tuple]:
    """``(seq, problem)`` when the checked pages prove ``evidence.reason``."""
    reason = evidence.reason
    if reason == Reason.STATEMENT_INCONSISTENT:
        if len(statements) == 1:
            return _chain_problem(statements[0])
        if len(statements) == 2:
            return _equivocation(statements[0], statements[1])
        return None
    if any(_chain_problem(statement) for statement in statements):
        # A page that does not hold together proves an inconsistency, not this reason.
        return None
    if reason == Reason.KILLED_WITH_MU:
        found = killed_with_mu(_rows(statements))
        return found[0] if found else None
    if reason == Reason.STATEMENT_CONTRADICTS_RECEIPT:
        return _contradiction(evidence, accused, statements)
    found = _quote_and_charges(statements)
    if found is None:
        return None
    quote_row, charges = found
    if reason == Reason.MAINTENANCE_OVERCHARGED:
        return statement_check.maintenance_overcharge(
            charges[1:], statement_check.MaintenanceQuote.of_row(quote_row), charges[0].ts)
    if reason in (Reason.INSTANCE_HIBERNATED_UNANNOUNCED, Reason.INSTANCE_STOPPED_UNANNOUNCED):
        faults = closing_check.judge(charges, quote_row.get("quote_on_close"),
                                     quote_row.get("quote_closes_at"))
        fault = next((f for f in faults if f.reason == reason), None)
        return None if fault is None else (fault.seq, fault.problem)
    return None


def _contradiction(evidence, accused, statements) -> Optional[tuple]:
    from src.identity import receipt_statement

    if not evidence.receipts:
        return None
    instance_ref = statements[0].instance_ref
    if not instance_ref:
        return None
    # Each receipt must be valid and name the execution of the same account, so a
    # receipt of another instance cannot make a finding.
    for receipt in evidence.receipts:
        if execution_receipt.problems(receipt, peer_id=accused, instance_ref=instance_ref):
            return None
    report = receipt_statement.check(list(evidence.receipts), _rows(statements),
                                     peer_id=accused)
    for finding in report["findings"]:
        if finding["kind"] != receipt_statement.INVALID_RECEIPT:
            return int(finding["seq"]), f"{finding['kind']}: {finding['detail']}"
    return None


def check(evidence: celaut_pb2.FaultEvidence) -> Optional[Proven]:
    """The fault that ``evidence`` proves against ``evidence.accused``, or None.

    Every statement and every receipt must be signed by the accused, and all the
    statements must be of one account. Never raises.
    """
    try:
        if evidence.reason not in PROVABLE:
            return None
        accused = (evidence.accused or "").lower()
        statements = list(evidence.statements)
        if not accused or not statements or len(statements) > MAX_STATEMENTS \
                or len(evidence.receipts) > MAX_RECEIPTS:
            return None
        account_ref = statements[0].account_ref
        if not account_ref or any(s.account_ref != account_ref for s in statements):
            return None
        if any(s.instance_ref != statements[0].instance_ref for s in statements):
            return None
        if not all(statement_signature.verify(s, accused) for s in statements):
            return None
        statements.sort(key=lambda s: (s.from_seq, s.ts))
        found = _prove(evidence, accused, statements)
        if found is None:
            return None
        seq, problem = found
        return Proven(evidence.reason, accused, account_ref, int(seq), str(problem))
    except Exception:
        return None


def evidence_of(reason: str, accused: str,
                statements: Iterable[Optional[celaut_pb2.BalanceStatement]],
                receipts: Iterable[celaut_pb2.ExecutionReceipt] = ()) -> celaut_pb2.FaultEvidence:
    """A ``FaultEvidence`` of these messages. A None statement is left out."""
    return celaut_pb2.FaultEvidence(
        reason=reason, accused=accused,
        statements=[s for s in statements if s is not None],
        receipts=list(receipts))


def attach(evidence: celaut_pb2.FaultEvidence) -> Optional[celaut_pb2.FaultEvidence]:
    """``evidence`` when :func:`check` proves its fault from it, else None.

    The reporter judged with what it knows (a mark, its clock), and the evidence alone may
    not show the same thing. Evidence that the receiver cannot check is never sent.
    """
    return evidence if check(evidence) is not None else None


def digest(evidence: celaut_pb2.FaultEvidence) -> str:
    """Blake2b-256 hex of the signed digests of the evidence: one per statement and per
    receipt, in order. Two copies of the same evidence have the same digest."""
    import hashlib

    parts = [evidence.reason, evidence.accused]
    parts += [statement_signature.statement_digest(s) for s in evidence.statements]
    parts += [execution_receipt.receipt_digest(r) for r in evidence.receipts]
    return hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=32).hexdigest()

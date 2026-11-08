"""The one check of execution receipts against a balance statement (#419, #631).

A node signs two accounts of one execution: the receipts that it gives while it runs
(``execution_receipt``), and the rows of the chain of the account that pays for it (the
balance statement). They must agree, as ``docs/EXECUTION_RECEIPTS.md`` says
("Receipts and the balance statement"):

* The ``start`` row has ``detail.execution_name``, the name of the receipts, and its
  amount is the ``initial_mu`` of the ``start`` receipt.
* The ``tunnel`` rows of a session have the same name and ``detail.session``. Their sum is
  the ``mu_charged`` of the ``close`` of that stream (at least that of the last checkpoint
  when the stream was cancelled and has no ``close``).
* The last ``tunnel`` row of a session moves nothing and holds ``detail.receipt_digest``,
  the digest of the ``close`` receipt, and ``detail.mu_charged``. The signed chain commits
  to the signed receipt.

Two signatures of one node that disagree are a proof that the node lied in one of them,
and anybody can check it. The node of a reader judges its peers with this function
(``balance_statement._judge_receipt``), ``nodo receipt verify --statement`` and
``nodo statement check-receipts`` print it, and the TUI shows what they print: one check,
not three.

The function takes rows, and not a statement, so that the node can give the rows of a
statement that a peer signed (``statement_check.rows_of``) and the CLI the rows of its own
database. A row is anything with ``seq``, ``ts``, ``amount``, ``kind`` and ``detail``.

What it does not judge: a session whose first row (the opening) is not among the rows
(the range of a statement can start after it), and a receipt with no row of its execution.
Those are ``skipped``, with the reason.
"""

from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence

from protos import celaut_pb2
from src.identity import execution_receipt

# The ``kind`` of the rows that this check reads: the same strings as ``ledger.START`` and
# ``ledger.TUNNEL``. Not imported, so that this module needs nothing of the database.
START = "start"
TUNNEL = "tunnel"

# A node writes the row that anchors a ``close`` just after it signs it. A later row of the
# account, this many seconds after the close, shows that the account went on with no anchor.
ANCHOR_GRACE_SECONDS = 60

# The kinds of ``Finding.kind``.
START_AMOUNT = "start_amount"
SESSION_TOTAL = "session_total"
SESSION_BELOW_CHECKPOINT = "session_below_checkpoint"
ANCHOR_DIGEST = "anchor_digest"
ANCHOR_TOTAL = "anchor_total"
ANCHOR_MISSING = "anchor_missing"
INVALID_RECEIPT = "invalid_receipt"


class Finding(NamedTuple):
    """One place where a receipt and the rows do not agree."""
    kind: str
    seq: int          # the row where it shows (0: no row, the receipt is the problem)
    receipt_kind: str
    session: str
    detail: str

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._asdict())


def _detail(row) -> Dict[str, Any]:
    detail = getattr(row, "detail", None)
    return detail if isinstance(detail, dict) else {}


def _total_charged(rows: Sequence) -> int:
    """What the rows took from the account: the sum of what they charged, less what they
    gave back (the refund of an opening that the next node refused)."""
    return -sum(int(row.amount) for row in rows)


def _check_start(receipt, rows: Sequence, findings: List[Finding], skipped: List[str]) -> None:
    mine = [r for r in rows if r.kind == START
            and _detail(r).get("execution_name") == receipt.execution_name]
    if not mine:
        skipped.append(f"start {receipt.execution_name[:12]}: no start row of that name")
        return
    row = mine[0]
    if int(row.amount) != int(receipt.initial_mu.n):
        findings.append(Finding(
            START_AMOUNT, int(row.seq), receipt.kind, "",
            f"the start receipt says {receipt.initial_mu.n} MU, row {row.seq} says "
            f"{int(row.amount)} MU"))


def _check_session(session: str, receipts: Sequence, rows: Sequence,
                   findings: List[Finding], skipped: List[str]) -> None:
    name = receipts[0].execution_name
    session_rows = [r for r in rows if r.kind == TUNNEL
                    and _detail(r).get("execution_name") == name
                    and _detail(r).get("session") == session]
    charges = [r for r in session_rows if not _detail(r).get("close")]
    anchors = [r for r in session_rows if _detail(r).get("close")]
    if not charges and not anchors:
        skipped.append(f"session {session[:12]}: no row of that session")
        return
    if charges and not any(_detail(r).get("open") for r in charges):
        skipped.append(f"session {session[:12]}: the opening of the session is not among "
                       f"the rows")
        return
    total = _total_charged(charges)
    close = next((r for r in receipts if r.kind == execution_receipt.KIND_CLOSE), None)
    last = max(receipts, key=lambda r: int(r.seq))
    if close is not None:
        if total != int(close.mu_charged.n):
            findings.append(Finding(
                SESSION_TOTAL, int(max(r.seq for r in charges)) if charges else 0,
                close.kind, session,
                f"the close receipt says {close.mu_charged.n} MU, the rows of the session "
                f"add up to {total} MU"))
    elif total < int(last.mu_charged.n):
        findings.append(Finding(
            SESSION_BELOW_CHECKPOINT, int(max(r.seq for r in charges)) if charges else 0,
            last.kind, session,
            f"the checkpoint {last.seq} says {last.mu_charged.n} MU, the rows of the "
            f"session add up to only {total} MU"))
    if close is None:
        return
    digest = execution_receipt.receipt_digest(close)
    if anchors:
        anchor = anchors[-1]
        if _detail(anchor).get("receipt_digest") != digest:
            findings.append(Finding(
                ANCHOR_DIGEST, int(anchor.seq), close.kind, session,
                f"row {anchor.seq} holds the digest "
                f"{str(_detail(anchor).get('receipt_digest'))[:16]}, the close receipt "
                f"has {digest[:16]}"))
        elif str(_detail(anchor).get("mu_charged")) != str(int(close.mu_charged.n)):
            findings.append(Finding(
                ANCHOR_TOTAL, int(anchor.seq), close.kind, session,
                f"row {anchor.seq} says {_detail(anchor).get('mu_charged')} MU charged, "
                f"the close receipt says {close.mu_charged.n} MU"))
    else:
        later = [r for r in rows if int(r.ts) > int(close.ts) + ANCHOR_GRACE_SECONDS]
        if later:
            findings.append(Finding(
                ANCHOR_MISSING, int(later[0].seq), close.kind, session,
                f"the account went on at row {later[0].seq} and the close of the session "
                f"has no row that anchors it"))


def check(receipts: Iterable["celaut_pb2.ExecutionReceipt"], rows: Sequence,
          peer_id: Optional[str] = None) -> Dict[str, Any]:
    """Check ``receipts`` against ``rows``.

    ``receipts`` are the receipts that one node signed, of any executions. A receipt that
    is not valid (``execution_receipt.problems``, with ``peer_id`` when it is given) is a
    finding of its own, and is not read further. ``rows`` are the rows of the account that
    pays, in order.

    Returns ``{"valid", "findings", "checked", "skipped"}``: ``valid`` when there is no
    finding, ``checked`` the number of receipts that were matched with rows, ``skipped``
    the reasons for what was not judged.
    """
    findings: List[Finding] = []
    skipped: List[str] = []
    usable = []
    for receipt in receipts:
        problems = execution_receipt.problems(receipt, peer_id=peer_id)
        if problems:
            findings.append(Finding(
                INVALID_RECEIPT, 0, receipt.kind, receipt.session,
                "; ".join(problems)))
        else:
            usable.append(receipt)

    rows = list(rows)
    checked = 0
    sessions: Dict[tuple, List] = {}
    for receipt in usable:
        if receipt.kind == execution_receipt.KIND_START:
            before = len(skipped)
            _check_start(receipt, rows, findings, skipped)
            checked += int(len(skipped) == before)
        elif receipt.plane == execution_receipt.PLANE_TUNNEL:
            sessions.setdefault((receipt.execution_name, receipt.session), []).append(receipt)
        else:
            skipped.append(f"{receipt.plane} receipt {receipt.session[:12]}: nothing in the "
                           f"ledger to check it against")
    for (_, session), group in sessions.items():
        before = len(skipped)
        _check_session(session, group, rows, findings, skipped)
        checked += int(len(skipped) == before)
    return {
        "valid": not findings,
        "findings": [f.as_dict() for f in findings],
        "checked": checked,
        "skipped": skipped,
    }

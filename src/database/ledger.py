"""The ledger of balance movements (#570).

Every balance on this node -- a client, a local instance, a delegated instance -- is
an *account*, and every change of it is one row of ``balance_movements``, written in
the same transaction as the balance. So the rows of an account add up to its balance
by construction, and the owner of the account can read them (``GetBalanceStatement``)
to check each charge against the rule that made it.

The account id is the id that the owner holds: the ``client_id``, or the instance
token that ``StartService`` returned. For a delegated instance that token is the
alias this node hands out (``delegated_instances.id``), not the token of the peer.

A row is never changed. Each one carries the hash of the row before it (``prev_hash``)
and its own (``hash``), so the rows of an account are a chain: the hash of the last
one commits to all the history, and a node that rewrites a row it already showed
cannot keep the chain. The first row hangs from a genesis hash that is tied to the
account. A start charges the father before the child exists, so the ``child_start``
row is written with no child, and the child is bound later with a new row,
``child_bound``, that names the ``child_start`` it completes. A reader treats a
``child_start`` with no ``child_bound`` and no ``refund`` as a start in progress.

A row never carries a credential. An instance token and a client_id are bearer
credentials, so a row names an instance by ``instance_ref`` (a hash of the token, with
a prefix of its own), and a statement names its account by ``account_ref``.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from src.utils.config import ConfigManager

env_manager = ConfigManager()

# What a movement is. Stable strings: they are signed in every statement, and a peer
# checks each one against its own rule.
DEPOSIT = "deposit"
"""A payment on a ledger, credited to a client (``Payable``)."""
CREDIT = "credit"
"""MU that nobody paid in: the free credit of a new client, the funding of a dev
client, or the operator with ``nodo credit_client``. ``detail.reason`` says which."""
DEBIT = "debit"
"""The operator took MU from a client with ``nodo credit_client --decrement``."""
START = "start"
"""The ``initial_mu`` that a new instance received from its father.
``detail.execution_name`` is the name that the execution receipts of the instance carry
(#419), when the node has an identity key."""
BUILD = "build"
"""The build of the service, taken from the ``initial_mu`` of a new instance."""
MAINTENANCE = "maintenance"
"""One maintenance period of an instance."""
CHILD_START = "child_start"
"""The account paid the ``initial_mu`` of a child. The child is named by the
``child_bound`` that follows once it exists. ``detail.start_ref`` is the StartReference
of the request, in hex, when the request had one (#578)."""
TOP_UP = "top_up"
"""``ModifyDeposit`` with a positive difference: negative on the father, positive on
the child."""
WITHDRAWAL = "withdrawal"
"""``ModifyDeposit`` with a negative difference: negative on the child, positive on
the father."""
REFUND = "refund"
"""MU that goes back to the father: what a stopped child did not use (negative on the
child, positive on the father), or a charge given back because the start failed."""
TUNNEL = "tunnel"
"""A ``ServiceTunnel``: the opening (``detail.open``), or the traffic (``detail.bytes``).
``detail.execution_name`` names the execution as its receipts do (#419), and
``detail.session`` is the receipt stream of the tunnel when the caller asked for
receipts: the sum of the rows of one session is the ``mu_charged`` of its ``close``."""
RESOURCE_CHANGE = "resource_change"
"""``ModifyServiceSystemResources``."""
STATEMENT_QUERY = "statement_query"
"""A ``GetBalanceStatement`` that the account asked for."""
CHILD_BOUND = "child_bound"
"""The child of a ``child_start`` exists. Moves nothing: ``instance_ref`` is the child
and ``detail.starts_seq`` the seq of the ``child_start`` that it completes."""

KINDS = (
    DEPOSIT, CREDIT, DEBIT, START, BUILD, MAINTENANCE, CHILD_START,
    CHILD_BOUND, TOP_UP, WITHDRAWAL, REFUND, TUNNEL, RESOURCE_CHANGE, STATEMENT_QUERY,
)

_MOVEMENT_PREFIX = "celaut-balance-movement:"
_GENESIS_PREFIX = "celaut-balance-genesis:"
_INSTANCE_PREFIX = "celaut-ledger-instance:"
_ACCOUNT_PREFIX = "celaut-ledger-account:"


def _digest(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def instance_ref(token: str) -> str:
    """What a row says about an instance: a hash of its token, never the token.

    The prefix keeps it from being any other credential. In particular it is not
    ``sha256(token)``, which the alias of a delegated instance used to be.
    """
    return _digest(_INSTANCE_PREFIX + token)


def account_ref(account: str) -> str:
    """What a statement says about its account: a hash of the client_id or token."""
    return _digest(_ACCOUNT_PREFIX + account)


def genesis_hash_of_ref(ref: str) -> str:
    """The ``prev_hash`` of the first row of the account that ``account_ref`` names."""
    return _digest(_GENESIS_PREFIX + ref)


def genesis_hash(account: str) -> str:
    """The ``prev_hash`` of the first row of an account."""
    return genesis_hash_of_ref(account_ref(account))


def canonical_amount(value) -> str:
    """An amount as the text that is hashed: a decimal integer, no sign but ``-``."""
    return str(int(value))


def movement_hash(prev_hash: str, seq: int, ts: int, amount_mu, balance_after_mu,
                  kind: str, instance_ref_: Optional[str], detail: Optional[str]) -> str:
    """The hash of a row: its fields, and the hash of the row before it.

    Every free text goes in as hexadecimal, so that no field can pass for a separator
    and two different rows never give the same text.
    """
    return _digest(
        f"{_MOVEMENT_PREFIX}{prev_hash}|{int(seq)}~{int(ts)}~"
        f"{_hex(canonical_amount(amount_mu))}~{_hex(canonical_amount(balance_after_mu))}~"
        f"{_hex(kind)}~{instance_ref_ or ''}~{_hex(detail or '')}"
    )

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MIN_PEER_RETENTION_DAYS = 7
DEFAULT_MAX_STATEMENT_PAGES_PER_TICK = 4
DEFAULT_STATEMENT_READ_EVERY_TICKS = 30
DEFAULT_MAX_STATEMENT_ROWS = 1000


class NoSuchAccount(Exception):
    """No client, live local instance or live delegated instance has this id."""


class InsufficientBalance(Exception):
    """The account does not hold the amount, and the node does not allow debt."""


@dataclass(frozen=True)
class Movement:
    """One movement to write with a new account, in order."""
    kind: str
    amount_mu: int
    instance_token: Optional[str] = None
    detail: Optional[Mapping[str, Any]] = None


def canonical_detail(detail: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The cause of a movement as the text that is stored and signed.

    Sorted keys and no spaces, so the same cause is always the same bytes. MU values
    go in as strings: a balance can exceed what a JSON reader holds in an integer.
    """
    if not detail:
        return None
    return json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _positive_int(key: str, default: int) -> int:
    try:
        value = int(env_manager.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def retention_days() -> int:
    """``ledger.RETENTION_DAYS``: rows older than this are deleted."""
    return _positive_int("ledger.RETENTION_DAYS", DEFAULT_RETENTION_DAYS)


def min_peer_retention_days() -> int:
    """``ledger.MIN_PEER_RETENTION_DAYS``: the retention that a peer is held to.

    A peer announces how long it keeps its rows. A gap in its statement is excused by
    that retention, and a peer that announces none would excuse any gap.
    """
    return _positive_int("ledger.MIN_PEER_RETENTION_DAYS", DEFAULT_MIN_PEER_RETENTION_DAYS)


def max_statement_pages_per_tick() -> int:
    """``ledger.MAX_STATEMENT_PAGES_PER_TICK``: the pages that one tick reads of an account."""
    return _positive_int("ledger.MAX_STATEMENT_PAGES_PER_TICK", DEFAULT_MAX_STATEMENT_PAGES_PER_TICK)


def statement_read_every_ticks(tick_seconds: float) -> int:
    """``ledger.STATEMENT_READ_EVERY_TICKS``: the ticks between two reads of our account on
    a peer.

    A read is a purchase, so it is not made at every tick. The rows are kept for the
    retention of the peer, and reading less often only delays a detection. The interval
    is held to a tenth of the shortest retention that a peer is held to: a node that read
    too rarely would meet gaps that its own slowness caused.
    """
    every = _positive_int("ledger.STATEMENT_READ_EVERY_TICKS", DEFAULT_STATEMENT_READ_EVERY_TICKS)
    longest = (min_peer_retention_days() * 86400) / 10
    return max(1, min(every, int(longest // max(float(tick_seconds), 1.0))))


def max_statement_rows() -> int:
    """``ledger.MAX_STATEMENT_ROWS``: the most rows that one statement returns."""
    return _positive_int("ledger.MAX_STATEMENT_ROWS", DEFAULT_MAX_STATEMENT_ROWS)

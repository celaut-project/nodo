"""The ledger of balance movements (#570).

Every balance on this node -- a client, a local instance, a delegated instance -- is
an *account*, and every change of it is one row of ``balance_movements``, written in
the same transaction as the balance. So the rows of an account add up to its balance
by construction, and the owner of the account can read them (``GetBalanceStatement``)
to check each charge against the rule that made it.

The account id is the id that the owner holds: the ``client_id``, or the instance
token that ``StartService`` returned. For a delegated instance that token is the
alias this node hands out (``delegated_instances.id``), not the token of the peer.

A row is never changed, with one exception: ``instance_token`` can go from empty to
set, one time. A start charges the father before the child exists, so the
``child_start`` row is written without the token of the child, and the token is set
when the child is created. A reader treats an empty ``instance_token`` on a
``child_start`` as a start in progress.
"""
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
"""The ``initial_mu`` that a new instance received from its father."""
BUILD = "build"
"""The build of the service, taken from the ``initial_mu`` of a new instance."""
COMMISSION = "commission"
"""The commission of this node on a delegated start or tick (#551)."""
MAINTENANCE = "maintenance"
"""One maintenance period of an instance."""
CHILD_START = "child_start"
"""The account paid the ``initial_mu`` of a child. ``instance_token`` is the child."""
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
"""A ``ServiceTunnel``: the opening, or the traffic."""
RESOURCE_CHANGE = "resource_change"
"""``ModifyServiceSystemResources``."""
STATEMENT_QUERY = "statement_query"
"""A ``GetBalanceStatement`` that the account asked for."""

KINDS = (
    DEPOSIT, CREDIT, DEBIT, START, BUILD, COMMISSION, MAINTENANCE, CHILD_START,
    TOP_UP, WITHDRAWAL, REFUND, TUNNEL, RESOURCE_CHANGE, STATEMENT_QUERY,
)

DEFAULT_RETENTION_DAYS = 30
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


def max_statement_rows() -> int:
    """``ledger.MAX_STATEMENT_ROWS``: the most rows that one statement returns."""
    return _positive_int("ledger.MAX_STATEMENT_ROWS", DEFAULT_MAX_STATEMENT_ROWS)

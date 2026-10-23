"""Why a reputation score moved.

Stable strings, not free text and not an enum of the moment: they are written to
`reputation_events` and read back by the detail views, which group by them. Renaming
one silently splits a subject's history in two, so add rather than rename.

Each is a statement about the *counterparty*, which is what keeps a score meaningful.
Nothing here describes something this node did to itself: an instance of ours that
the service lost scores the service it ran, never the peer that happened to host it.
An instance that runs out of balance scores nothing, because the father holds its balance.
"""


class Reason:
    # Peers.
    PAYMENT_COMMUNICATED = "payment_communicated"
    """The peer took our `Payable` call and credited the deposit."""

    PAYMENT_UNACKNOWLEDGED = "payment_unacknowledged"
    """We paid on-chain and the peer never acknowledged it. Money out, no balance."""

    PAYMENT_CALL_FAILED = "payment_call_failed"
    """One `Payable` attempt failed. Charged per attempt, so it is small."""

    PEER_REFRESH_FAILED = "peer_refresh_failed"
    """The peer could not answer `GetPeerInfo` at any address we hold for it."""

    GET_SERVICE_TIMED_OUT = "get_service_timed_out"
    """The peer's `GetService` went silent, or did not finish in time, while sending us
    a service. Scored once per service asked for, not once per retry."""

    GET_SERVICE_WRONG_HASH = "get_service_wrong_hash"
    """The peer answered `GetService` with a service that does not hash to the id we
    asked for. Dropped, never stored; scored far below a timeout."""

    STATEMENT_INCONSISTENT = "statement_inconsistent"
    """A balance statement of the peer does not continue its chain: a gap in the
    movements, a movement that does not hang from the one before it or does not take the
    balance where it says, movements that it showed before and changed, a signature that
    does not verify, or no statement at all from a peer that answers (#570)."""

    STATEMENT_OVERCHARGED = "statement_overcharged"
    """A balance statement of the peer shows a charge on our account that this node did
    not cause: a start that is not one of ours, a top-up of an instance that is not ours,
    or more statements than this node asked for (#570)."""

    # Services.
    INSTANCE_LOST = "instance_lost"
    """The instance's virtual machine no longer exists and had to be pruned."""

    INSTANCE_OUT_OF_BALANCE = "instance_out_of_balance"
    """The instance could not pay for the interval it had just used.

    Retired: the node does not write this reason any more (#562). The father holds the
    balance of the instance and can empty it on purpose, so the death is not a fault of
    the service. The constant stays because old rows of `reputation_events` hold the
    string, and a reader must still find its name."""

    # There is deliberately no reason for "the interval was charged and nothing went
    # wrong". The maintenance tick runs every ten seconds, so scoring a routine success
    # would earn a service running for a month some +2.6M and drown every penalty it
    # ever took. A successful interval is the absence of a problem, not a judgement.

    # Not written from Python.
    OPERATOR_ADJUSTMENT = "operator_adjustment"
    """Someone moved a peer's score by hand, with +/- in the TUI. Written there, in
    Rust (`app.rs::adjust_peer_reputation`), against this same string -- so a hand
    adjustment is not the one unexplained step in a peer's history."""

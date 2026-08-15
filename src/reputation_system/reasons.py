"""Why a reputation score moved.

Stable strings, not free text and not an enum of the moment: they are written to
`reputation_events` and read back by the detail views, which group by them. Renaming
one silently splits a subject's history in two, so add rather than rename.

Each is a statement about the *counterparty*, which is what keeps a score meaningful.
Nothing here describes something this node did to itself: an instance of ours running
out of balance is our client's doing and scores the service it ran, never the peer
that happened to host it.
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

    # Services.
    INSTANCE_LOST = "instance_lost"
    """The instance's virtual machine no longer exists and had to be pruned."""

    INSTANCE_OUT_OF_BALANCE = "instance_out_of_balance"
    """The instance could not pay for the interval it had just used."""

    INTERVAL_CHARGED = "interval_charged"
    """The instance paid for the interval it had just used."""

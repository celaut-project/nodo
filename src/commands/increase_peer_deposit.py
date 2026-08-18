from src.utils.java_dependency import JavaDependencyMissing
from src.utils.monetary import format_mu, parse_to_mu


def increase_peer_deposit(peer_id, amount):
    """
    Increases this node's deposit on a peer.

    :param peer_id: The ID of the peer to deposit with.
    :param amount: The amount to add, in the operator's display unit
        (`ui.DISPLAY_UNIT`, ERG by default).
    """
    try:
        amount_mu = parse_to_mu(amount)
    except ValueError as exc:
        print(f"Invalid amount: {exc}")
        return

    try:
        from src.payment_system.payment_process import (
            deposit_refusal_reason,
            increase_deposit_on_peer,
        )

        # Why it cannot be sent is more use than that it was not: below the
        # ledger's minimum output, or no payment system shared with this peer.
        refusal = deposit_refusal_reason(peer_id, amount_mu)
        if refusal:
            print(f"Cannot deposit on peer {peer_id}: {refusal}.")
            return

        result = increase_deposit_on_peer(peer_id=peer_id, amount=amount_mu)
        if result:
            print(f"Successfully increased the deposit on peer {peer_id} by {format_mu(amount_mu)}.")
        else:
            print(f"Failed to increase the deposit on peer {peer_id}.")
    except JavaDependencyMissing as exc:
        print(str(exc))

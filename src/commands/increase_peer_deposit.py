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
        from src.payment_system.payment_process import increase_deposit_on_peer

        result = increase_deposit_on_peer(peer_id=peer_id, amount=amount_mu)
        if result:
            print(f"Successfully increased the deposit on peer {peer_id} by {format_mu(amount_mu)}.")
        else:
            print(f"Failed to increase the deposit on peer {peer_id}.")
    except JavaDependencyMissing as exc:
        print(str(exc))

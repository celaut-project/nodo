from src.utils.java_dependency import JavaDependencyMissing
from src.utils.monetary import erg_to_mu, mu_to_erg_str


def increase_peer_deposit(peer_id, erg):
    """
    Increases this node's deposit on a peer by the specified amount of ERG.

    :param peer_id: The ID of the peer to deposit with.
    :param erg: The amount to add, as a decimal ERG string.
    """
    try:
        amount_mu = erg_to_mu(erg)
    except ValueError as exc:
        print(f"Invalid ERG amount: {exc}")
        return

    try:
        from src.payment_system.payment_process import increase_deposit_on_peer

        result = increase_deposit_on_peer(peer_id=peer_id, amount=amount_mu)
        if result:
            print(f"Successfully increased the deposit on peer {peer_id} by {mu_to_erg_str(amount_mu)} ERG.")
        else:
            print(f"Failed to increase the deposit on peer {peer_id}.")
    except JavaDependencyMissing as exc:
        print(str(exc))

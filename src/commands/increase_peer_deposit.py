from src.utils.java_dependency import JavaDependencyMissing


def increase_peer_deposit(peer_id, gas):
    """
    Increases the gas of a specific peer by the specified amount.
    
    :param peer_id: The ID of the peer whose gas is to be increased.
    :param gas: The amount of gas to add to the peer's balance.
    """
    try:
        from src.payment_system.payment_process import increase_deposit_on_peer

        result = increase_deposit_on_peer(peer_id=peer_id, amount=gas)
        if result:
            print(f"Successfully increased gas for peer {peer_id} by {gas}.")
        else:
            print(f"Failed to increase gas for peer {peer_id}.")
    except JavaDependencyMissing as exc:
        print(str(exc))

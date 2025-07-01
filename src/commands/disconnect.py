from src.database.sql_connection import SQLConnection


sc = SQLConnection()

def disconnect(peer_id: str):
    """
    Disconnects from a peer with the given peer_id.
    
    Args:
        peer_id (str): The ID of the peer to disconnect from.
    """
    if not sc.peer_exists(peer_id):
        raise Exception(f"Peer with ID {peer_id} does not exist.")

    print(f"Disconnecting from peer with ID {peer_id}...")

    sc.remove_peer(peer_id)
    print(f"Successfully disconnected from peer with ID {peer_id}.")
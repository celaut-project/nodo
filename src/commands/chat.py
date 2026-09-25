"""``nodo chat`` -- send and read the free-text channel between peer operators.

Thin CLI wrapper: all of the signing, verification and storage is
``src.manager.chat``; this only prints.
"""
from datetime import datetime, timezone


def send_chat(peer_id: str, body: str) -> bool:
    """Send ``body`` to ``peer_id``. Returns whether it was sent."""
    from src.manager.chat import ChatError, send_chat_message

    try:
        send_chat_message(peer_id=peer_id, body=body)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    except Exception as e:
        print(f"Could not reach peer {peer_id}: {e}", flush=True)
        return False
    print(f"Sent to {peer_id}.", flush=True)
    return True


def show_chat(peer_id: str, limit: int = 100) -> bool:
    """Print the stored conversation with ``peer_id``, oldest first."""
    from src.manager.chat import get_chat_history

    history = get_chat_history(peer_id=peer_id, limit=limit)
    if not history:
        print(f"No stored messages with {peer_id}.", flush=True)
        return True
    for entry in history:
        when = datetime.fromtimestamp(entry["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        who = "us" if entry["from_us"] else peer_id
        print(f"[{when}] {who}: {entry['body']}", flush=True)
    return True

"""``nodo chat`` -- send and read the free-text channel between peer operators.

Thin CLI wrapper: all of the signing, verification and storage is
``src.manager.chat``; this only prints. Conversations (issue #431) are an
addition, not a replacement: the flat, un-threaded ``send_chat``/``show_chat``
below are unchanged from before they existed.
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
        thread = f" (thread {entry['conversation_id']})" if entry.get("conversation_id") else ""
        print(f"[{when}] {who}{thread}: {entry['body']}", flush=True)
    return True


def open_thread(peer_id: str, topic: str) -> bool:
    """Open a new conversation with ``peer_id``, sending ``topic`` as its first message."""
    from src.manager.chat import ChatError, close_conversation, open_conversation, send_chat_message

    try:
        conversation_id = open_conversation(peer_id=peer_id, topic=topic)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    try:
        send_chat_message(peer_id=peer_id, body=topic, conversation_id=conversation_id)
    except ChatError as e:
        # The thread exists locally either way -- opening is not the send -- but a
        # thread whose first message never went anywhere is confusing left open.
        close_conversation(conversation_id)
        print(f"STOP: {e}", flush=True)
        return False
    except Exception as e:
        close_conversation(conversation_id)
        print(f"Could not reach peer {peer_id}: {e}", flush=True)
        return False
    print(f"Opened {conversation_id} with {peer_id}.", flush=True)
    return True


def reply_in_thread(conversation_id: str, body: str) -> bool:
    """Send ``body`` in the existing conversation ``conversation_id``."""
    from src.manager.chat import ChatError, reply_to_conversation

    try:
        reply_to_conversation(conversation_id=conversation_id, body=body)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    except Exception as e:
        print(f"Could not reach the peer: {e}", flush=True)
        return False
    print(f"Sent in {conversation_id}.", flush=True)
    return True


def list_threads(peer_id: str = None) -> bool:
    """Print every conversation (open and closed) this node knows, newest first."""
    from src.manager.chat import list_conversations

    conversations = list_conversations(peer_id=peer_id)
    if not conversations:
        print("No conversations." if peer_id is None else f"No conversations with {peer_id}.", flush=True)
        return True
    for entry in conversations:
        status = "closed" if entry["closed_at"] else "open"
        opener = "us" if entry["opened_by_us"] else entry["peer_id"]
        topic = f" -- {entry['topic']}" if entry["topic"] else ""
        print(
            f"{entry['id']} [{status}] with {entry['peer_id']}, opened by {opener}"
            f" at {entry['opened_at']}{topic}",
            flush=True,
        )
    return True


def show_thread(conversation_id: str, limit: int = 200) -> bool:
    """Print one conversation's messages, oldest first."""
    from src.manager.chat import get_conversation_history

    history = get_conversation_history(conversation_id=conversation_id, limit=limit)
    if not history:
        print(f"No stored messages in {conversation_id}.", flush=True)
        return True
    for entry in history:
        when = datetime.fromtimestamp(entry["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        who = "us" if entry["from_us"] else "them"
        print(f"[{when}] {who}: {entry['body']}", flush=True)
    return True


def close_thread(conversation_id: str) -> bool:
    from src.manager.chat import ChatError, close_conversation

    try:
        close_conversation(conversation_id)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    print(f"Closed {conversation_id}.", flush=True)
    return True


def reopen_thread(conversation_id: str) -> bool:
    from src.manager.chat import ChatError, reopen_conversation

    try:
        reopen_conversation(conversation_id)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    print(f"Reopened {conversation_id}.", flush=True)
    return True

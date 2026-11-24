"""``nodo chat_*`` -- talk to the operator of a node, and answer the clients of this one.

Thin CLI wrapper: all of the storage, the checks and the network are
``src.manager.chat``; this only prints. A conversation is named ``client_id#number`` (see
that module). The same commands work on both kinds: a conversation that a client of this
node opened (answer it) and one that this node opened on a peer (write to that peer).
"""
from datetime import datetime, timezone


def _service_suffix(entry: dict) -> str:
    """How a stored service card (issue #438) reads in a plain-text transcript."""
    service = entry.get("service")
    if not service:
        return ""
    tags = f" ({', '.join(service['tags'])})" if service["tags"] else ""
    # No id: its Metadata has no hash of this node's registry type (see
    # registry_service_id), so there is nothing here to `nodo get` it by.
    service_id = service["id"] or "(no id of this node's hash type)"
    return f" [service {service_id}{tags}]"


def _emit(document: dict) -> bool:
    from src.commands._catalogue import emit_json
    emit_json(document)
    return True


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _sync_quietly(peer_id: str = None) -> None:
    """Pull the answers of the peers before a read, so that the read is current.

    A peer that does not answer must not stop the read: the stored copy is still shown.
    """
    from src.manager.chat import sync_all

    try:
        sync_all(peer_id=peer_id)
    except Exception:
        pass


def open_thread(peer_id: str, topic: str, body: str = None, service: str = None,
                as_json: bool = False) -> bool:
    """Open a conversation with the operator of ``peer_id``, sending ``body`` first.

    ``body`` defaults to ``topic``. This node is a client of that peer, and the peer
    gives the number of the conversation.
    """
    from src.manager.chat import ChatError, open_conversation, parse_ref

    if body is None:
        body = topic
    try:
        ref = open_conversation(peer_id=peer_id, topic=topic, body=body, service=service)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    except Exception as e:
        print(f"Could not reach peer {peer_id}: {e}", flush=True)
        return False
    if as_json:
        client_id, number = parse_ref(ref)
        return _emit({
            "ref": ref, "client_id": client_id, "number": number,
            "peer_id": peer_id, "topic": topic,
        })
    print(f"Opened {ref} with {peer_id}.", flush=True)
    return True


def reply_in_thread(ref: str, body: str, service: str = None) -> bool:
    """Send ``body`` in the conversation ``ref``, whichever side it is on."""
    from src.manager.chat import ChatError, reply

    try:
        reply(ref=ref, body=body, service=service)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    except Exception as e:
        print(f"Could not reach the peer: {e}", flush=True)
        return False
    print(f"Sent in {ref}.", flush=True)
    return True


def list_threads(peer_id: str = None, as_json: bool = False) -> bool:
    """Print every conversation (open and closed), the most recently active first.

    ``waiting`` marks the conversations whose last message is not from the operator of
    the node that serves them: for a conversation of a client it is the client's, so the
    operator of this node must answer; for a conversation that this node opened it is our
    own, so the peer has not answered yet.
    """
    from src.manager.chat import list_conversations

    _sync_quietly(peer_id=peer_id)
    conversations = list_conversations(peer_id=peer_id)
    if as_json:
        return _emit({"conversations": conversations})
    if not conversations:
        print("No conversations." if peer_id is None else f"No conversations with {peer_id}.", flush=True)
        return True
    for entry in conversations:
        status = "closed" if entry["closed_at"] else "open"
        waiting = "" if entry["last_from_operator"] else ", waiting"
        side = "ours (we are the client)" if entry["opened_by_us"] else "a client's"
        peer = entry["peer_id"] or "no peer"
        topic = f" -- {entry['topic']}" if entry["topic"] else ""
        print(
            f"{entry['ref']} [{status}{waiting}] {side}, {peer}, "
            f"{entry['messages']} message(s), last {_when(entry['last_ts'])}{topic}",
            flush=True,
        )
    return True


def show_thread(ref: str, limit: int = 200, as_json: bool = False) -> bool:
    """Print the messages of one conversation, oldest first."""
    from src.manager.chat import ChatError, get_conversation, get_conversation_history

    try:
        conversation = get_conversation(ref)
        if conversation is not None and conversation["opened_by_us"]:
            _sync_quietly(peer_id=conversation["peer_id"])
        history = get_conversation_history(ref=ref, limit=limit)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    if as_json:
        return _emit({"ref": ref, "messages": history})
    if not history:
        print(f"No stored messages in {ref}.", flush=True)
        return True
    mirror = bool(conversation and conversation["opened_by_us"])
    for entry in history:
        # The operator of the node that serves the conversation wrote `from_operator`.
        # On a mirror that is the other side, not us.
        ours = (not entry["from_operator"]) if mirror else entry["from_operator"]
        who = "us" if ours else "them"
        print(
            f"[{_when(entry['ts'])}] #{entry['sequence']} {who}: "
            f"{entry['body']}{_service_suffix(entry)}",
            flush=True,
        )
    return True


def close_thread(ref: str) -> bool:
    from src.manager.chat import ChatError, close_conversation

    try:
        close_conversation(ref)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    print(f"Closed {ref}.", flush=True)
    return True


def reopen_thread(ref: str) -> bool:
    from src.manager.chat import ChatError, reopen_conversation

    try:
        reopen_conversation(ref)
    except ChatError as e:
        print(f"STOP: {e}", flush=True)
        return False
    print(f"Reopened {ref}.", flush=True)
    return True


def sync_chats(peer_id: str = None) -> bool:
    """Copy the new messages of the conversations that this node opened on its peers."""
    from src.manager.chat import sync_all

    copied = sync_all(peer_id=peer_id)
    print(f"{copied} new message(s).", flush=True)
    return True

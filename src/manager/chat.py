"""Chat between a client and the operator of a node (issue: peer chat; issue #431).

A free-text channel outside any service execution. Without it, there is no way for a
client to reach the operator of a node when something about a shared instance or a
payment goes wrong, and the operator cannot answer.

**One kind of conversation.** A conversation is always opened by a client against a
node, and it lives on that node. Its name is ``client_id#number``: ``client_id`` is the
id that the node gave to the client, ``number`` is 1 for the first conversation of that
client, then 2, 3, and so on. The node gives the number, and it never reuses one, so the
name is unique in the node and both sides use it. A message names a conversation by
``number``; ``0`` opens a new one.

The operator of a node that wants to talk to the operator of another node is a client of
that other node (the client_id that it holds there, ``peer.remote_client_id``), the same
as any other client. So the node keeps two kinds of rows, in the same tables:

* *served*: a conversation of one of its own clients. The operator answers it
  (:func:`reply`). A client that is bound to a peer is a client like any other.
* *mirror*: a conversation that this node opened as a client of another node. It lives
  on that node. The rows here are a local copy, so that the operator reads them with no
  network, and :func:`sync_all` fills them with the answers (``GetChatThread``).

**Binding a client to a peer** (``associate_client_with_peer``) changes nothing in the
conversations. The peer of a served conversation is read from the binding when needed
(the view ``chat_threads``), so the conversations that exist stay the same and a reader
groups them by peer.

**How a client learns about an answer.** The node never sends it: the client reads the
messages with ``GetChatThread`` and a cursor, the ``sequence`` of the last message that
it has. ``ChatThread.latest`` tells at once if there is something new. For a mirror,
:func:`sync_all` does this for every peer; the daemon runs it on a timer and the commands
that read a conversation run it first.

Authentication is the same as every other client-facing RPC of the gateway: ``client_id``
is a bearer credential, like ``TokenMessage.token``. A peer that wants to be recognised
signs over the client_id in the request that creates it (``Client.peer_id`` and
``Client.signature``, ``node_identity.client_binding_payload``), not in ``Chat``: a
signature in ``Chat`` would have put a bearer secret inside a message that others may
relay.
"""
from __future__ import annotations

import os
import time
from typing import List, Optional, Tuple

from protos import celaut_pb2
from src.database.sql_connection import SQLConnection
from src.identity.grpc_transport import peer_channel
from src.utils.bee_client import BeeClient
from src.manager.manager import get_client_id_on_other_peer
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.verify import registry_service_id

env_manager = ConfigManager()
sc = SQLConnection()

MAX_MESSAGE_BYTES = int(env_manager.get("chat.MAX_MESSAGE_BYTES", 4096) or 4096)
# The most messages that the node keeps for one client, over all its conversations.
MAX_STORED_MESSAGES_PER_CLIENT = int(
    env_manager.get("chat.MAX_STORED_MESSAGES_PER_CLIENT", 200) or 200
)
# How often the daemon copies the answers of the peers (see :func:`sync_tick`).
SYNC_INTERVAL_SECONDS = int(env_manager.get("chat.SYNC_INTERVAL_SECONDS", 30) or 30)
MAX_TOPIC_CHARS = 120
# The most messages one GetChatThread answer holds.
MAX_THREAD_ENTRIES_PER_ANSWER = 200

# The shape a shared service's Metadata (issue #438, `ChatMessage.service`) must
# have to be stored. It comes from a peer, so it is bounded like a body is: a cap on
# its serialized size (which also bounds everything nested in it), and hashes and
# tags that are few and short -- the parts the card shows and `get` is handed.
MAX_SERVICE_METADATA_BYTES = 32 * 1024
MAX_SERVICE_HASHES = 8
MAX_SERVICE_HASH_BYTES = 64
MAX_SERVICE_TAGS = 16
MAX_SERVICE_TAG_CHARS = 64


class ChatError(Exception):
    """A Chat message was refused: unknown client_id or conversation, or a bad body."""


def format_ref(client_id: str, number: int) -> str:
    """The name of a conversation: ``client_id#number``."""
    return f"{client_id}#{int(number)}"


def parse_ref(ref: str) -> Tuple[str, int]:
    """The ``(client_id, number)`` of a name ``client_id#number``. Raises :class:`ChatError`."""
    client_id, separator, number = (ref or "").rpartition("#")
    if not separator or not client_id or not number.isdigit() or int(number) < 1:
        raise ChatError(f"{ref!r} is not a conversation. A conversation is client_id#number.")
    return client_id, int(number)


def _validated_body(body: str) -> str:
    if not body:
        raise ChatError("Message is empty.")
    if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ChatError(
            f"Message is over the {MAX_MESSAGE_BYTES}-byte limit; send something shorter."
        )
    return body


def service_fallback_line(metadata: celaut_pb2.Metadata) -> str:
    """The line a shared service travels as in ``body``, for a reader without cards.

    A node that predates ``ChatMessage.service`` skips the field and shows only the
    body, so the body has to name the service on its own -- by the sender's registry
    id, the one ``nodo get`` there took. A reader that has the Metadata strips this
    exact line back off (:func:`_strip_fallback`) rather than show the service twice.
    """
    return _fallback_line(registry_service_id(metadata) or "", list(metadata.hashtag.tag))


def _fallback_line(service_id: str, tags: List[str]) -> str:
    tagged = f" ({', '.join(tags)})" if tags else ""
    return f"[service {service_id}{tagged} -- get it with: nodo get {service_id}]"


def _strip_fallback(body: str, metadata: celaut_pb2.Metadata) -> str:
    # The sender named the service by *its* registry hash type, which need not be
    # this node's, so the line may carry any of the Metadata's hashes.
    for hash in metadata.hashtag.hash:
        line = _fallback_line(hash.value.hex(), list(metadata.hashtag.tag))
        if body == line:
            return ""
        if body.endswith("\n" + line):
            return body[: -len(line) - 1]
    return body


def _validated_service(metadata: celaut_pb2.Metadata) -> celaut_pb2.Metadata:
    if metadata.ByteSize() > MAX_SERVICE_METADATA_BYTES:
        raise ChatError(
            f"The shared service's metadata is over the {MAX_SERVICE_METADATA_BYTES}-byte limit."
        )
    hashes = metadata.hashtag.hash
    if not hashes or len(hashes) > MAX_SERVICE_HASHES or any(
        not hash.type or not hash.value
        or len(hash.type) > MAX_SERVICE_HASH_BYTES or len(hash.value) > MAX_SERVICE_HASH_BYTES
        for hash in hashes
    ):
        raise ChatError(
            f"The shared service must carry 1 to {MAX_SERVICE_HASHES} hashes, each a type "
            f"and a value of up to {MAX_SERVICE_HASH_BYTES} bytes."
        )
    tags = metadata.hashtag.tag
    if len(tags) > MAX_SERVICE_TAGS or any(
        not tag.strip() or len(tag) > MAX_SERVICE_TAG_CHARS or "\n" in tag for tag in tags
    ):
        raise ChatError(
            f"The shared service's tags must be at most {MAX_SERVICE_TAGS} single-line "
            f"tags of up to {MAX_SERVICE_TAG_CHARS} characters."
        )
    return metadata


def local_service_metadata(service: str) -> celaut_pb2.Metadata:
    """The Metadata of a service in this node's own registry, by id or tag.

    The same Metadata ``nodo services``, ``get`` and ``execute`` read, sent as it is.
    Only a service this node holds can be shared, and only if its Metadata names it
    by the id the registry keys it under -- the card is a promise that the Metadata
    leads to something real, and the one place that can be checked is here.
    """
    from src.commands.__by_tag import get_id
    from src.utils.utils import read_metadata_from_disk

    registry = env_manager.get("REGISTRY")
    service_id = (get_id(service) or service or "").lower()
    if not service_id or not os.path.isdir(os.path.join(registry, service_id)):
        raise ChatError(f"{service} is not a service in the local registry.")
    metadata = read_metadata_from_disk(service_hash=service_id)
    if metadata is None or registry_service_id(metadata) != service_id:
        raise ChatError(f"{service}'s metadata does not name it by its registry id.")
    return _validated_service(metadata)


def _validated_topic(topic: Optional[str]) -> str:
    topic = " ".join((topic or "").split())
    if len(topic) > MAX_TOPIC_CHARS:
        raise ChatError(f"The topic is over {MAX_TOPIC_CHARS} characters; use a shorter one.")
    return topic


# ---------------------------------------------------------------------------------------
# The node that serves the conversations.


def receive_chat_message(message: celaut_pb2.ChatMessage) -> Tuple[int, int]:
    """Verify and store an incoming Chat message. Return ``(conversation_id, sequence)``.

    Raises :class:`ChatError` when ``client_id`` is not a client of this node, the
    conversation is not one of that client, or the body or the shared service is not
    valid. Nothing is stored in that case, and no conversation is opened.

    ``conversation_id`` 0 opens a new conversation of the client with the next number
    (``topic`` is its label). A message in a conversation that the operator closed opens
    it again: the client waits for an answer.
    """
    client_id = message.client_id
    if not client_id or not sc.client_exists(client_id=client_id):
        raise ChatError("Unknown client_id.")

    body = _validated_body(message.body)
    # A shared service (issue #438). Validated before anything is stored, like the
    # body: malformed Metadata refuses the whole message rather than storing a card
    # the TUI would then offer to `get`.
    service = _validated_service(message.service) if message.HasField("service") else None
    if service is not None:
        body = _strip_fallback(body, service)

    number = int(message.conversation_id)
    if number:
        row = sc.get_chat_thread_row(client_id=client_id, number=number)
        if row is None or row["opened_by_us"]:
            raise ChatError(f"Unknown conversation {format_ref(client_id, number)}.")
        if row["closed_at"]:
            sc.reopen_chat_conversation(client_id=client_id, number=number)
    else:
        topic = _validated_topic(message.topic) if message.HasField("topic") else ""
        number = sc.open_chat_conversation(client_id=client_id, topic=topic)

    sequence = sc.add_chat_message(
        client_id=client_id, conversation=number, from_operator=False, body=body,
        ts=int(time.time()), keep_per_client=MAX_STORED_MESSAGES_PER_CLIENT, service=service,
    )
    log.LOGGER(f"Chat message {sequence} stored in {format_ref(client_id, number)}.")
    return number, sequence


def get_client_thread(client_id: str, after_sequence: int = 0) -> celaut_pb2.ChatThread:
    """The ChatThread of ``client_id`` for ``GetChatThread``.

    The messages with a sequence over ``after_sequence``, in order, of all the
    conversations of the client. At most ``MAX_THREAD_ENTRIES_PER_ANSWER``: if there
    are more, these are the first ones after the cursor (not the newest), so a client
    that moves its cursor to the last sequence that it got reads everything. ``latest``
    is the last sequence of the client whatever the cut, 0 if it has no message.
    """
    thread = celaut_pb2.ChatThread(latest=sc.get_chat_latest(client_id=client_id))
    for entry in sc.get_chat_messages_after(
        client_id=client_id, after_sequence=after_sequence,
        limit=MAX_THREAD_ENTRIES_PER_ANSWER,
    ):
        added = thread.entries.add(
            sequence=entry["sequence"], conversation_id=entry["conversation"],
            ts=entry["ts"], from_operator=entry["from_operator"], body=entry["body"],
        )
        if entry["service_metadata"] is not None:
            added.service.ParseFromString(entry["service_metadata"])
    return thread


def _reply_as_operator(client_id: str, number: int, body: str,
                       service: Optional[str] = None) -> int:
    """Store the answer of the operator in a served conversation; return its sequence.

    Nothing is sent: the client reads it with ``GetChatThread``.
    """
    row = sc.get_chat_thread_row(client_id=client_id, number=number)
    if row is None or row["opened_by_us"]:
        raise ChatError(f"{format_ref(client_id, number)} is not a conversation of a client of this node.")
    if row["closed_at"]:
        raise ChatError(f"{format_ref(client_id, number)} is closed; reopen it before replying.")
    metadata = local_service_metadata(service) if service else None
    if body or metadata is None:
        _validated_body(body)
    sequence = sc.add_chat_message(
        client_id=client_id, conversation=number, from_operator=True, body=body,
        ts=int(time.time()), keep_per_client=MAX_STORED_MESSAGES_PER_CLIENT, service=metadata,
    )
    log.LOGGER(f"Chat answer {sequence} stored in {format_ref(client_id, number)}.")
    return sequence


# ---------------------------------------------------------------------------------------
# The node that is the client (a mirror of a conversation that lives on another node).


def _client_id_on(peer_id: str) -> str:
    """The client_id that this node holds on ``peer_id``; it becomes a client if it has none."""
    if not sc.peer_exists(peer_id=peer_id):
        raise ChatError(f"{peer_id} is not a known peer.")
    client_id = sc.get_peer_client(peer_id=peer_id)
    if not client_id:
        try:
            # Also the call that lets the peer bind that new client_id to this node.
            client_id = get_client_id_on_other_peer(peer_id=peer_id)
        except Exception as e:
            raise ChatError(f"Could not become a client of {peer_id}: {e}") from e
    if not client_id:
        raise ChatError(f"Could not become a client of {peer_id}.")
    return client_id


def _send_to_peer(peer_id: str, client_id: str, conversation_id: int, body: str,
                  service: Optional[str] = None, topic: str = "") -> celaut_pb2.ChatAck:
    """Send one message to ``peer_id`` in a conversation of ``client_id``; return its ack."""
    metadata = local_service_metadata(service) if service else None
    wire_body = body
    if metadata is not None:
        line = service_fallback_line(metadata)
        wire_body = f"{body}\n{line}" if body else line
    _validated_body(wire_body)
    topic = _validated_topic(topic)

    chat_message = celaut_pb2.ChatMessage(
        client_id=client_id, conversation_id=int(conversation_id), body=wire_body,
    )
    if topic and not conversation_id:
        chat_message.topic = topic
    if metadata is not None:
        chat_message.service.CopyFrom(metadata)

    ack = BeeClient.chat(peer_channel(peer_id=peer_id), chat_message)
    if ack is None or not ack.stored:
        # The reason of the peer travels back as it is: the node of the peer knows why.
        raise ChatError((ack.reason if ack is not None else "") or f"{peer_id} refused the message.")
    return ack


def open_conversation(peer_id: str, topic: str, body: str, service: Optional[str] = None) -> str:
    """Open a conversation with the operator of ``peer_id`` and send its first message.

    This node acts as a client of that peer. The peer gives the number; the conversation
    is then mirrored here. Returns the name ``client_id#number``. ``topic`` is a label for
    the lists of conversations, ``body`` the first message (it may be empty if
    ``service`` is given).
    """
    topic = _validated_topic(topic)
    client_id = _client_id_on(peer_id)
    ack = _send_to_peer(peer_id, client_id, 0, body, service=service, topic=topic)
    sc.open_chat_conversation(
        client_id=client_id, topic=topic, remote_peer_id=peer_id, number=ack.conversation_id,
    )
    _sync_after_send(client_id, peer_id)
    log.LOGGER(f"Opened {format_ref(client_id, ack.conversation_id)} with peer {peer_id}.")
    return format_ref(client_id, ack.conversation_id)


def _send_in_mirror(row: dict, body: str, service: Optional[str] = None) -> None:
    if row["closed_at"]:
        raise ChatError(f"{row['ref']} is closed; reopen it before replying.")
    _send_to_peer(row["peer_id"], row["client_id"], row["number"], body, service=service)
    _sync_after_send(row["client_id"], row["peer_id"])


def _sync_after_send(client_id: str, peer_id: str) -> None:
    # The message is already stored on the peer. If the read fails now, the next sync
    # brings it, so a failure here is not an error of the send.
    try:
        _sync_client(client_id, peer_id)
    except Exception as e:
        log.LOGGER(f"Chat sync with peer {peer_id} failed after a send: {e}")


def _sync_client(client_id: str, peer_id: str) -> int:
    """Copy the new messages of ``client_id`` on ``peer_id`` to the mirror. Return how many."""
    channel = peer_channel(peer_id=peer_id)
    cursor = sc.get_chat_latest(client_id=client_id)
    copied = 0
    while True:
        thread = BeeClient.get_chat_thread(channel, client_id, after_sequence=cursor)
        if thread is None or not thread.entries:
            break
        for entry in thread.entries:
            cursor = max(cursor, entry.sequence)
            if not entry.conversation_id or not entry.sequence:
                continue
            service = None
            if entry.HasField("service"):
                try:
                    service = _validated_service(entry.service)
                except ChatError:
                    service = None
            body = entry.body.encode("utf-8")[:MAX_MESSAGE_BYTES].decode("utf-8", "ignore")
            if service is not None:
                body = _strip_fallback(body, service)
            sc.open_chat_conversation(
                client_id=client_id, remote_peer_id=peer_id, number=entry.conversation_id,
            )
            sc.add_chat_message(
                client_id=client_id, conversation=entry.conversation_id,
                from_operator=entry.from_operator, body=body, ts=entry.ts,
                keep_per_client=MAX_STORED_MESSAGES_PER_CLIENT, sequence=entry.sequence,
                service=service,
            )
            copied += 1
        if cursor >= thread.latest:
            break
    return copied


def sync_all(peer_id: Optional[str] = None) -> int:
    """Copy the new messages of every mirror (or of one peer's) here. Return how many.

    A peer that does not answer is skipped, and the others still sync.
    """
    copied = 0
    for mirrored in sc.get_mirrored_clients():
        if peer_id is not None and mirrored["peer_id"] != peer_id:
            continue
        try:
            copied += _sync_client(mirrored["client_id"], mirrored["peer_id"])
        except Exception as e:
            log.LOGGER(f"Chat sync with peer {mirrored['peer_id']} failed: {e}")
    return copied


_last_sync = 0.0


def sync_tick() -> None:
    """Copy the answers of the peers, at most once every ``SYNC_INTERVAL_SECONDS``.

    Called on every pass of the maintenance loop. It gates itself on the interval and
    never raises, like the other ticks, so a peer that is down cannot stop the loop.
    """
    global _last_sync
    now = time.monotonic()
    if now - _last_sync < SYNC_INTERVAL_SECONDS:
        return
    _last_sync = now
    try:
        sync_all()
    except Exception as e:
        log.LOGGER(f"Chat sync failed: {e}")


# ---------------------------------------------------------------------------------------
# What the operator does, whichever side the conversation is on.


def reply(ref: str, body: str, service: Optional[str] = None) -> None:
    """Send ``body`` in the conversation ``ref`` (``client_id#number``).

    On a served conversation this is the answer of the operator to a client. On a mirror
    this node writes as the client, to the peer where the conversation lives.
    """
    client_id, number = parse_ref(ref)
    row = sc.get_chat_thread_row(client_id=client_id, number=number)
    if row is None:
        raise ChatError(f"No such conversation: {ref}.")
    if row["opened_by_us"]:
        _send_in_mirror(row, body, service=service)
    else:
        _reply_as_operator(client_id, number, body, service=service)


def close_conversation(ref: str) -> None:
    """Mark a conversation closed. Local only -- the other side is never told."""
    client_id, number = parse_ref(ref)
    if sc.get_chat_thread_row(client_id=client_id, number=number) is None:
        raise ChatError(f"No such conversation: {ref}.")
    if not sc.close_chat_conversation(client_id=client_id, number=number):
        raise ChatError(f"Could not close conversation {ref}.")


def reopen_conversation(ref: str) -> None:
    """Undo :func:`close_conversation`, so replying in it is allowed again."""
    client_id, number = parse_ref(ref)
    if sc.get_chat_thread_row(client_id=client_id, number=number) is None:
        raise ChatError(f"No such conversation: {ref}.")
    if not sc.reopen_chat_conversation(client_id=client_id, number=number):
        raise ChatError(f"Could not reopen conversation {ref}.")


def list_conversations(peer_id: Optional[str] = None, opened_by_us: Optional[bool] = None,
                       include_closed: bool = True) -> List[dict]:
    """The conversations, the most recently active first. See :meth:`SQLConnection.list_chat_threads`."""
    return sc.list_chat_threads(
        peer_id=peer_id, opened_by_us=opened_by_us, include_closed=include_closed,
    )


def get_conversation_history(ref: str, limit: int = 200) -> List[dict]:
    """The stored messages of one conversation, oldest first."""
    client_id, number = parse_ref(ref)
    return sc.get_conversation_messages(client_id=client_id, conversation=number, limit=limit)


def get_conversation(ref: str) -> Optional[dict]:
    """One conversation as ``chat_threads`` shows it, or None."""
    client_id, number = parse_ref(ref)
    return sc.get_chat_thread_row(client_id=client_id, number=number)

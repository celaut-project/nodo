"""Peer-to-peer operator chat (issue: peer chat; conversations, issue #431).

A free-text channel between two node operators, outside any service execution --
there is otherwise no way for one operator to reach the other at all when
something about a shared instance or payment goes wrong.

Authenticated the same way every other client-facing RPC on this gateway is:
``client_id`` is a bearer credential, exactly like ``TokenMessage.token`` or
``ModifyDepositInput.service_token``, not a fresh signature scheme of its own.
This node attributes a Chat message to whichever peer it already knows that
client_id as (``peer.local_client_id``) -- a peer this node has never introduced
itself with cannot chat, because it can never have gotten a bound client_id in
the first place (see below).

The association itself is made once, at ``GenerateClient`` time
(``manager._created_client``), not here: a client_id is single-use and minted
right there, so a peer that wants to be recognised later signs over it -- with
its identity key, the same one a ``Peer`` announcement is signed with -- in the
very request that creates it (``Client.peer_id``/``Client.signature``,
``node_identity.client_binding_payload``). Putting that signature on Chat
itself, instead, was the first cut of this design and was wrong: it would have
meant broadcasting an internal client_id UUID inside a message this node also
lets its neighbours relay -- exactly the bearer secret Chat depends on to
authenticate, handed to anyone in earshot of the gossip.

**Conversations (issue #431) are a `conversation_id` per message, not a live
connection.** A thread is any run of messages that carry the same id -- a UUID4
the side opening the topic picks, exactly the way a `client_id` is picked -- not
a `Chat` stream held open between two daemons for the topic's lifetime. Tying a
thread to one connection would make it synchronous: both operators would need
their node's stream open at the same moment for the thread to exist at all, which
does not fit the motivating case ("tell the other operator something is wrong")
-- the recipient is very often not watching their node right then. A message
naming no `conversation_id` is exactly the flat, un-threaded history this had
before conversations existed, and still works unchanged.
"""
# Deferred: `celaut_pb2.ChatMessage` only exists once `bash/generate_protos.sh` has
# been rerun against the new RPC (see that script's docstring); this keeps a plain
# `import src.manager.chat` from failing on the type hint below in the meantime.
from __future__ import annotations

import os
import time
from typing import List, Optional
from uuid import uuid4

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
MAX_STORED_MESSAGES_PER_PEER = int(
    env_manager.get("chat.MAX_STORED_MESSAGES_PER_PEER", 200) or 200
)

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
    """A Chat message was refused: unknown or unassociated client_id, or oversize."""


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


def receive_chat_message(message: celaut_pb2.ChatMessage) -> str:
    """Verify, store and return the peer_id of an incoming Chat message.

    Raises :class:`ChatError` when ``client_id`` is not a client of this node, or
    is one this node never associated with a peer (see the module docstring for
    where that association is made), or the body is empty/oversize.

    A ``conversation_id`` this node has not seen before opens the thread on this
    side too (``opened_by_us=False``): the peer picked the id, so from here it
    reads as one of *our clients* reaching out, which is exactly the reverse-
    direction TUI page (issue #431) this distinction exists for.
    """
    client_id = message.client_id
    if not client_id or not sc.client_exists(client_id=client_id):
        raise ChatError("Unknown client_id.")

    peer_id = sc.get_peer_id_by_local_client(client_id=client_id)
    if not peer_id:
        raise ChatError(
            f"client_id {client_id} is not associated with any peer; call "
            "GenerateClient asserting your peer identity first."
        )

    body = _validated_body(message.body)

    # A shared service (issue #438). Validated before anything is stored, like the
    # body: malformed Metadata refuses the whole message rather than storing a card
    # the TUI would then offer to `get`.
    service = _validated_service(message.service) if message.HasField("service") else None
    if service is not None:
        body = _strip_fallback(body, service)

    conversation_id = message.conversation_id if message.HasField("conversation_id") else None
    if conversation_id:
        sc.create_conversation(conversation_id=conversation_id, peer_id=peer_id, opened_by_us=False)

    sc.add_chat_message(
        peer_id=peer_id, from_us=False, body=body, ts=int(time.time()),
        keep_per_peer=MAX_STORED_MESSAGES_PER_PEER, conversation_id=conversation_id,
        service=service,
    )
    log.LOGGER(f"Chat message stored from peer {peer_id} (client {client_id}).")
    return peer_id


def send_chat_message(peer_id: str, body: str, conversation_id: Optional[str] = None,
                      service: Optional[str] = None) -> None:
    """Send and locally record a Chat message to ``peer_id``.

    Sent under whichever client_id this node already holds on ``peer_id``
    (``sc.get_peer_client`` -- the existing, unrelated ``remote_client_id``); when
    there is none yet, this node becomes a client of that peer first
    (``get_client_id_on_other_peer``), which is also the call that lets the
    recipient bind that new client_id back to *this* node's own peer_id.

    ``conversation_id`` must already be a thread this node opened
    (:func:`open_conversation`) -- passed through as-is, never minted here, so a
    typo names a thread that fails to record rather than silently starting a new
    one under a name nobody asked for.

    ``service`` (an id or tag in the local registry) attaches that service's
    Metadata as a card (issue #438). The body may then be empty: the card is the message, and the
    body on the wire still names the service for a peer that predates cards
    (:func:`service_fallback_line`).
    """
    metadata = local_service_metadata(service) if service else None
    wire_body = body
    if metadata is not None:
        line = service_fallback_line(metadata)
        wire_body = f"{body}\n{line}" if body else line
    _validated_body(wire_body)

    if not sc.peer_exists(peer_id=peer_id):
        raise ChatError(f"{peer_id} is not a known peer.")

    if conversation_id:
        conversation = sc.get_conversation(conversation_id)
        if conversation is None or conversation["peer_id"] != peer_id:
            raise ChatError(f"{conversation_id} is not a conversation with {peer_id}.")
        if conversation["closed_at"]:
            raise ChatError(f"{conversation_id} is closed; reopen it before replying.")

    client_id = sc.get_peer_client(peer_id=peer_id)
    if not client_id:
        try:
            client_id = get_client_id_on_other_peer(peer_id=peer_id)
        except Exception as e:
            raise ChatError(f"Could not become a client of {peer_id}: {e}") from e
    if not client_id:
        raise ChatError(f"Could not become a client of {peer_id}.")

    chat_message = celaut_pb2.ChatMessage(client_id=client_id, body=wire_body)
    if conversation_id:
        chat_message.conversation_id = conversation_id
    if metadata is not None:
        chat_message.service.CopyFrom(metadata)

    ack = BeeClient.chat(peer_channel(peer_id=peer_id), chat_message)
    if ack is not None and not ack.stored:
        # The peer's own reason (unknown/unassociated client_id there, an empty or
        # oversize body) travels back verbatim rather than being reworded: it is
        # the recipient's node that knows which, not this one.
        raise ChatError(ack.reason or f"{peer_id} refused the message.")

    sc.add_chat_message(
        peer_id=peer_id, from_us=True, body=body, ts=int(time.time()),
        keep_per_peer=MAX_STORED_MESSAGES_PER_PEER, conversation_id=conversation_id,
        service=metadata,
    )
    log.LOGGER(f"Chat message sent to peer {peer_id}.")


def open_conversation(peer_id: str, topic: str = "") -> str:
    """Start a new thread with ``peer_id``, returning its ``conversation_id``.

    Purely local bookkeeping: opening does not itself send anything, and the id
    is not shared with the peer until the first :func:`send_chat_message` names
    it. ``topic`` is a free-text label for the TUI's conversation list, set once
    and never compared or enforced.
    """
    if not sc.peer_exists(peer_id=peer_id):
        raise ChatError(f"{peer_id} is not a known peer.")
    conversation_id = uuid4().hex
    if not sc.create_conversation(
        conversation_id=conversation_id, peer_id=peer_id, opened_by_us=True, topic=topic,
    ):
        raise ChatError(f"Could not open a conversation with {peer_id}.")
    return conversation_id


def close_conversation(conversation_id: str) -> None:
    """Mark a thread closed. Local only -- the peer is never told."""
    if not sc.close_conversation(conversation_id):
        raise ChatError(f"Could not close conversation {conversation_id}.")


def reopen_conversation(conversation_id: str) -> None:
    """Undo :func:`close_conversation`, so replying in it is allowed again."""
    if sc.get_conversation(conversation_id) is None:
        raise ChatError(f"No such conversation: {conversation_id}.")
    if not sc.reopen_conversation(conversation_id):
        raise ChatError(f"Could not reopen conversation {conversation_id}.")


def reply_to_conversation(conversation_id: str, body: str, service: Optional[str] = None) -> None:
    """Send ``body`` in ``conversation_id``, resolving its peer automatically.

    The convenience a caller who already has a ``conversation_id`` in hand wants
    -- one that came from :func:`list_conversations`, say -- over
    :func:`send_chat_message`, which needs the peer named explicitly.
    """
    conversation = sc.get_conversation(conversation_id)
    if conversation is None:
        raise ChatError(f"No such conversation: {conversation_id}.")
    send_chat_message(
        peer_id=conversation["peer_id"], body=body, conversation_id=conversation_id,
        service=service,
    )


def list_conversations(peer_id: Optional[str] = None, opened_by_us: Optional[bool] = None,
                       include_closed: bool = True) -> List[dict]:
    """Threads, most recently opened first. See :meth:`SQLConnection.list_conversations`."""
    return sc.list_conversations(
        peer_id=peer_id, opened_by_us=opened_by_us, include_closed=include_closed,
    )


def get_conversation_history(conversation_id: str, limit: int = 200) -> List[dict]:
    """The stored messages of one thread, oldest first."""
    return sc.get_conversation_messages(conversation_id=conversation_id, limit=limit)


def get_chat_history(peer_id: str, limit: int = 100) -> List[dict]:
    """The stored conversation with ``peer_id``, oldest first."""
    return sc.get_chat_messages(peer_id=peer_id, limit=limit)

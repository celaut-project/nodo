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

import time
from typing import List, Optional
from uuid import uuid4

from bee_rpc import client as bee

from protos import celaut_pb2, celaut_pb2_grpc
from src.database.sql_connection import SQLConnection
from src.identity.grpc_transport import peer_channel
from src.manager.manager import get_client_id_on_other_peer
from src.utils import logger as log
from src.utils.config import ConfigManager

env_manager = ConfigManager()
sc = SQLConnection()

MAX_MESSAGE_BYTES = int(env_manager.get("chat.MAX_MESSAGE_BYTES", 4096) or 4096)
MAX_STORED_MESSAGES_PER_PEER = int(
    env_manager.get("chat.MAX_STORED_MESSAGES_PER_PEER", 200) or 200
)


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

    conversation_id = message.conversation_id if message.HasField("conversation_id") else None
    if conversation_id:
        sc.create_conversation(conversation_id=conversation_id, peer_id=peer_id, opened_by_us=False)

    sc.add_chat_message(
        peer_id=peer_id, from_us=False, body=body, ts=int(time.time()),
        keep_per_peer=MAX_STORED_MESSAGES_PER_PEER, conversation_id=conversation_id,
    )
    log.LOGGER(f"Chat message stored from peer {peer_id} (client {client_id}).")
    return peer_id


def send_chat_message(peer_id: str, body: str, conversation_id: Optional[str] = None) -> None:
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
    """
    body = _validated_body(body)

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

    chat_message = celaut_pb2.ChatMessage(client_id=client_id, body=body)
    if conversation_id:
        chat_message.conversation_id = conversation_id

    ack = next(bee.client_grpc(
        method=celaut_pb2_grpc.GatewayStub(peer_channel(peer_id=peer_id)).Chat,
        input=chat_message,
        indices_parser=celaut_pb2.ChatAck,
        partitions_message_mode_parser=True,
    ), None)
    if ack is not None and not ack.stored:
        # The peer's own reason (unknown/unassociated client_id there, an empty or
        # oversize body) travels back verbatim rather than being reworded: it is
        # the recipient's node that knows which, not this one.
        raise ChatError(ack.reason or f"{peer_id} refused the message.")

    sc.add_chat_message(
        peer_id=peer_id, from_us=True, body=body, ts=int(time.time()),
        keep_per_peer=MAX_STORED_MESSAGES_PER_PEER, conversation_id=conversation_id,
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


def reply_to_conversation(conversation_id: str, body: str) -> None:
    """Send ``body`` in ``conversation_id``, resolving its peer automatically.

    The convenience a caller who already has a ``conversation_id`` in hand wants
    -- one that came from :func:`list_conversations`, say -- over
    :func:`send_chat_message`, which needs the peer named explicitly.
    """
    conversation = sc.get_conversation(conversation_id)
    if conversation is None:
        raise ChatError(f"No such conversation: {conversation_id}.")
    send_chat_message(peer_id=conversation["peer_id"], body=body, conversation_id=conversation_id)


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

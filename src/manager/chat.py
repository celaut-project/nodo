"""Peer-to-peer operator chat (issue: peer chat).

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
"""
# Deferred: `celaut_pb2.ChatMessage` only exists once `bash/generate_protos.sh` has
# been rerun against the new RPC (see that script's docstring); this keeps a plain
# `import src.manager.chat` from failing on the type hint below in the meantime.
from __future__ import annotations

import time
from typing import List

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

    sc.add_chat_message(
        peer_id=peer_id, from_us=False, body=body, ts=int(time.time()),
        keep_per_peer=MAX_STORED_MESSAGES_PER_PEER,
    )
    log.LOGGER(f"Chat message stored from peer {peer_id} (client {client_id}).")
    return peer_id


def send_chat_message(peer_id: str, body: str) -> None:
    """Send and locally record a Chat message to ``peer_id``.

    Sent under whichever client_id this node already holds on ``peer_id``
    (``sc.get_peer_client`` -- the existing, unrelated ``remote_client_id``); when
    there is none yet, this node becomes a client of that peer first
    (``get_client_id_on_other_peer``), which is also the call that lets the
    recipient bind that new client_id back to *this* node's own peer_id.
    """
    body = _validated_body(body)

    if not sc.peer_exists(peer_id=peer_id):
        raise ChatError(f"{peer_id} is not a known peer.")

    client_id = sc.get_peer_client(peer_id=peer_id)
    if not client_id:
        try:
            client_id = get_client_id_on_other_peer(peer_id=peer_id)
        except Exception as e:
            raise ChatError(f"Could not become a client of {peer_id}: {e}") from e
    if not client_id:
        raise ChatError(f"Could not become a client of {peer_id}.")

    chat_message = celaut_pb2.ChatMessage(client_id=client_id, body=body)

    next(bee.client_grpc(
        method=celaut_pb2_grpc.GatewayStub(peer_channel(peer_id=peer_id)).Chat,
        partitions_message_mode_parser=True,
        input=chat_message,
    ), None)

    sc.add_chat_message(
        peer_id=peer_id, from_us=True, body=body, ts=int(time.time()),
        keep_per_peer=MAX_STORED_MESSAGES_PER_PEER,
    )
    log.LOGGER(f"Chat message sent to peer {peer_id}.")


def get_chat_history(peer_id: str, limit: int = 100) -> List[dict]:
    """The stored conversation with ``peer_id``, oldest first."""
    return sc.get_chat_messages(peer_id=peer_id, limit=limit)

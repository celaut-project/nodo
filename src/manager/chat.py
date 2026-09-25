"""Peer-to-peer operator chat (issue: peer chat).

A free-text channel between two node operators, outside any service execution --
there is otherwise no way for one operator to reach the other at all when
something about a shared instance or payment goes wrong.

Authenticated the same way a ``Peer`` announces itself, because the gRPC server
gives no verified caller identity of its own (``grpc_transport``'s module
docstring): the sender signs ``peer_id|ts|body`` with its identity key
(``node_identity.chat_message_payload``), and only a peer this node has already
introduced itself with (``sc.peer_exists``) is accepted -- chat is a channel
between known peers, not an open inbox for strangers.

``ChatMessage.client_id`` is how the two ends of a peer's client relationship get
tied together (see the ``peer`` table's ``local_client_id`` comment in
``migrate.py``): when this node calls ``send_chat_message`` on a peer it already
holds a client_id on (``sc.get_peer_client``), it rides along on the message, and
the recipient -- if that client_id is one of its own -- records the association
via ``sc.set_peer_local_client``.
"""
# Deferred: `celaut_pb2.ChatMessage` only exists once `bash/generate_protos.sh` has
# been rerun against the new RPC (see that script's docstring); this keeps a plain
# `import src.manager.chat` from failing on the type hint below in the meantime.
from __future__ import annotations

import time
from typing import List, Optional

from bee_rpc import client as bee

from protos import celaut_pb2, celaut_pb2_grpc
from src.database.sql_connection import SQLConnection
from src.identity.grpc_transport import peer_channel
from src.identity.node_identity import (
    chat_message_payload,
    get_node_public_key_hex,
    normalize_public_key_hex,
    sign_peer_payload,
    verify_peer_payload,
)
from src.utils import logger as log
from src.utils.config import ConfigManager

env_manager = ConfigManager()
sc = SQLConnection()

MAX_MESSAGE_BYTES = int(env_manager.get("chat.MAX_MESSAGE_BYTES", 4096) or 4096)
MAX_STORED_MESSAGES_PER_PEER = int(
    env_manager.get("chat.MAX_STORED_MESSAGES_PER_PEER", 200) or 200
)


class ChatError(Exception):
    """A Chat message was refused: bad identity, unknown peer, oversize or stale."""


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

    Raises :class:`ChatError` on anything that keeps the message from being
    accepted: an unverifiable signature, a peer this node has never introduced
    itself with, an oversize body, or a ``ts`` that does not move this peer's
    chat history strictly forward (replay).
    """
    peer_id = normalize_public_key_hex(message.peer_id)
    if not peer_id:
        raise ChatError("peer_id is not a canonical public key.")

    if not verify_peer_payload(
        peer_id,
        chat_message_payload(peer_id, message.ts, message.body),
        message.signature,
    ):
        raise ChatError(f"Signature does not verify for claimed peer_id {peer_id}.")

    if not sc.peer_exists(peer_id=peer_id):
        raise ChatError(f"{peer_id} is not a known peer; IntroducePeer before chatting.")

    last_ts = sc.get_last_received_chat_ts(peer_id=peer_id)
    if last_ts is not None and message.ts <= last_ts:
        raise ChatError("Stale or replayed message (ts did not move forward).")

    body = _validated_body(message.body)

    client_id = message.client_id if message.HasField("client_id") else ""
    if client_id and sc.client_exists(client_id=client_id):
        sc.set_peer_local_client(peer_id=peer_id, client_id=client_id)

    sc.add_chat_message(
        peer_id=peer_id, from_us=False, body=body, ts=message.ts,
        keep_per_peer=MAX_STORED_MESSAGES_PER_PEER,
    )
    log.LOGGER(f"Chat message stored from peer {peer_id}.")
    return peer_id


def send_chat_message(peer_id: str, body: str) -> None:
    """Sign, send and locally record a Chat message to ``peer_id``."""
    body = _validated_body(body)

    if not sc.peer_exists(peer_id=peer_id):
        raise ChatError(f"{peer_id} is not a known peer.")

    our_id = get_node_public_key_hex()
    if not our_id:
        raise ChatError("This node has no identity key configured; cannot sign a chat message.")

    ts = int(time.time())
    signature = sign_peer_payload(chat_message_payload(our_id, ts, body))
    if not signature:
        raise ChatError("Could not sign the chat message: no identity key.")

    chat_message = celaut_pb2.ChatMessage(peer_id=our_id, ts=ts, body=body, signature=signature)
    client_id_on_recipient = sc.get_peer_client(peer_id=peer_id)
    if client_id_on_recipient:
        chat_message.client_id = client_id_on_recipient

    next(bee.client_grpc(
        method=celaut_pb2_grpc.GatewayStub(peer_channel(peer_id=peer_id)).Chat,
        partitions_message_mode_parser=True,
        input=chat_message,
    ), None)

    sc.add_chat_message(
        peer_id=peer_id, from_us=True, body=body, ts=ts,
        keep_per_peer=MAX_STORED_MESSAGES_PER_PEER,
    )
    log.LOGGER(f"Chat message sent to peer {peer_id}.")


def get_chat_history(peer_id: str, limit: int = 100) -> List[dict]:
    """The stored conversation with ``peer_id``, oldest first."""
    return sc.get_chat_messages(peer_id=peer_id, limit=limit)

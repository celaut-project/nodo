"""The signature of a chat entry, with no database.

A node that keeps a conversation signs each message that it stores (``ChatEntry``): the
messages of the client, as a receipt that the node got them, and the answers of the
operator. A client or an operator that holds the entries can show them to others as
evidence of what the node said and kept. The digest and the check are here, apart from the
node, so that a reader of an exported conversation checks it with no state of any node
(``nodo chat_verify``).

The signature covers a canonical text of every field, never ``SerializeToString()``:
protobuf serialization is not canonical. The celaut-gateway layer announces the same
encoding (``chat.entry.hash`` in transport_stack).

Each entry holds the hash of the entry before it in the same conversation (``prev_hash``),
so a node cannot remove or add a message in the middle without a break in the chain.

The signature binds the node and nobody else (docs/CHAT_EVIDENCE.md).
"""
import hashlib
from typing import Iterable, Optional, Tuple

from protos import celaut_pb2
from src.identity.node_identity import chat_entry_payload, verify_peer_payload

_CLIENT_REF_PREFIX = "celaut-chat-client:"
_GENESIS_PREFIX = "celaut-chat-genesis:"
_ENTRY_PREFIX = "celaut-chat-entry:"


def _blake(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def client_ref(client_id: str) -> str:
    """The hash that stands for ``client_id`` in a signed entry.

    Blake2b-256 hex of ``celaut-chat-client:`` followed by the client_id. A client_id is a
    credential, so it never goes into a signed conversation that can be shown to others.
    """
    return _blake(_CLIENT_REF_PREFIX + client_id)


def genesis_hash(client_ref_: str, conversation_id: int) -> str:
    """The ``prev_hash`` of the first message of a conversation."""
    return _blake(f"{_GENESIS_PREFIX}{client_ref_}#{int(conversation_id)}")


def service_part(service: Optional[celaut_pb2.Metadata]) -> str:
    """The part of the text for the service of a message: its hashes and its tags.

    ``type~value`` of each hash in hexadecimal, joined by ``/``; then ``~``; then the
    tags in hexadecimal, joined by ``/``. Empty when the message has no service. The rest
    of the Metadata is not signed.
    """
    if service is None:
        return ""
    hashes = "/".join(
        f"{h.type.hex()}~{h.value.hex()}" for h in service.hashtag.hash
    )
    tags = "/".join(_hex(tag) for tag in service.hashtag.tag)
    return f"{hashes}~{tags}"


def entry_hash(client_ref_: str, entry: celaut_pb2.ChatEntry) -> str:
    """Blake2b-256 hex of the canonical text of every field of the entry but the signature.

    The text is ``celaut-chat-entry:`` and the parts, joined by ``|``: the ``client_ref``
    (it is not a field: the entry does not carry the client), then the fields in the
    order of their numbers in ``celaut.proto``: ``sequence``, ``conversation_id``, ``ts``,
    ``from_operator`` (1 or 0), ``hex(body)``, the service part and ``prev_hash``. A part
    with no value is empty.
    """
    service = entry.service if entry.HasField("service") else None
    return _blake(_ENTRY_PREFIX + "|".join((
        client_ref_, str(entry.sequence), str(entry.conversation_id), str(entry.ts),
        "1" if entry.from_operator else "0", _hex(entry.body), service_part(service),
        entry.prev_hash,
    )))


def verify_entry(entry: celaut_pb2.ChatEntry, client_ref_: str, peer_id: str) -> bool:
    """Whether ``peer_id`` signed exactly this entry, for the client of ``client_ref_``."""
    return verify_peer_payload(
        peer_id, chat_entry_payload(peer_id, entry_hash(client_ref_, entry)), entry.signature,
    )


def verify_chain(entries: Iterable[celaut_pb2.ChatEntry], client_ref_: str,
                 peer_id: str) -> Tuple[bool, bool, str]:
    """Check the entries of one conversation, in order of sequence.

    Returns ``(valid, from_start, reason)``. ``valid`` is true if each entry has a valid
    signature, all are of the same conversation, the sequences grow, and each ``prev_hash``
    is the hash of the entry before. ``from_start`` is true if the first entry is the first
    message of the conversation (its ``prev_hash`` is the genesis hash). If it is not, the
    node removed the older messages (the cap of stored messages), and the first link
    cannot be checked: the entries are still signed. ``reason`` says what is wrong when
    ``valid`` is false.
    """
    previous: Optional[celaut_pb2.ChatEntry] = None
    previous_hash = ""
    from_start = False
    for entry in entries:
        if not verify_entry(entry, client_ref_, peer_id):
            return False, False, f"the signature of the entry {entry.sequence} is not valid"
        if previous is None:
            from_start = entry.prev_hash == genesis_hash(client_ref_, entry.conversation_id)
        else:
            if entry.conversation_id != previous.conversation_id:
                return False, from_start, f"the entry {entry.sequence} is of another conversation"
            if entry.sequence <= previous.sequence:
                return False, from_start, f"the sequence {entry.sequence} does not grow"
            if entry.prev_hash != previous_hash:
                return False, from_start, (
                    f"the entry {entry.sequence} does not follow the entry {previous.sequence}"
                )
        previous, previous_hash = entry, entry_hash(client_ref_, entry)
    return True, from_start, ""

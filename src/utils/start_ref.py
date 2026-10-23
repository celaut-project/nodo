"""The reference of a ``StartService`` request (#578).

This node chooses one for each start that it delegates. A reference is
``nonce || MAC(node key, nonce)``, so the node can tell its own references from any
other without a table: after a restart, or after a retry that also failed. The peer
writes the reference in the ``child_start`` that pays the start, and a ``child_start``
whose reference has no valid MAC is a charge that this node did not cause.
"""
import hashlib
import hmac
import os
import threading
from functools import lru_cache
from typing import Optional, Set

from cryptography.hazmat.primitives import serialization

from src.utils import logger as log

NONCE_BYTES = 16
MAC_BYTES = 16
# The most that a peer takes from a caller. A reference is stored and written in the
# ledger, so it is bounded.
MAX_BYTES = 64

_PERSONALISATION = b"nodo-start-ref"


@lru_cache(maxsize=4)
def _key_of(mnemonic: Optional[str]) -> bytes:
    from src.identity.node_identity import _cached_keypair
    if mnemonic:
        try:
            raw = _cached_keypair(mnemonic)[1].private_bytes(
                serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                serialization.NoEncryption())
            return hashlib.blake2b(raw, digest_size=32, person=_PERSONALISATION).digest()
        except ValueError as e:
            log.LOGGER(f"The identity of this node gives no key for start references: {e}")
    # With no identity, the references of this process are still its own. After a
    # restart, they are not recognised.
    return os.urandom(32)


def _key() -> bytes:
    from src.identity.node_identity import get_identity_mnemonic
    return _key_of(get_identity_mnemonic())


def _mac(nonce: bytes) -> bytes:
    return hmac.new(_key(), nonce, hashlib.sha256).digest()[:MAC_BYTES]


def new() -> bytes:
    """A new reference of this node."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce + _mac(nonce)


def ours(ref_hex: str) -> bool:
    """Whether ``ref_hex``, a reference in hex as the ledger holds it, is one of this
    node."""
    try:
        ref = bytes.fromhex(ref_hex or "")
    except (TypeError, ValueError):
        return False
    if len(ref) != NONCE_BYTES + MAC_BYTES:
        return False
    return hmac.compare_digest(ref[NONCE_BYTES:], _mac(ref[:NONCE_BYTES]))


# The references of the starts that ``delegate_execution`` still waits for in this
# process. A ``child_start`` with one of them has a child that is not an orphan yet.
_in_flight: Set[str] = set()
_lock = threading.Lock()


def begin(ref_hex: str) -> None:
    with _lock:
        _in_flight.add(ref_hex)


def end(ref_hex: str) -> None:
    with _lock:
        _in_flight.discard(ref_hex)


def in_flight(ref_hex: str) -> bool:
    with _lock:
        return ref_hex in _in_flight

"""The shared relay rule for ListPeers and proactive introductions.

A Peer signature covers *every* URI, including its expiry. If a relay removes a URI,
the signature becomes invalid. If it removes the signature, registration refuses the
announcement. Thus a relay sends the claim exactly as the peer signed it, or does
not send it. Private, loopback and DNS URIs travel as the peer declared them.
"""
import random
import time

from protos import celaut_pb2
from src.utils.logger import LOGGER


def gossip_limit(config, key: str, default: int) -> int:
    """Zero disables work; malformed numeric settings retain the shipped default."""
    try:
        return max(0, int(config.get(f"communication.{key}", default)))
    except (TypeError, ValueError, OverflowError):
        return default


def relayable_peer(peer, peer_id: str, now=None) -> bool:
    """Is this intact advertisement signed by its subject and still useful?"""
    if (not isinstance(peer, celaut_pb2.Peer) or not peer_id
            or peer.public_key != peer_id or not peer.signature or not peer.uri):
        return False
    now = time.time() if now is None else now
    return any(not uri.expiry_unix_timestamp or uri.expiry_unix_timestamp > now
               for uri in peer.uri)


def iter_gossip_peers(sc, limit: int, exclude_peer_id=None):
    """Read stored signed claims, never reconstructed/merged URI-table rows.

    Bad rows cost one candidate, not the entire response. A mismatched row identity
    is skipped, not downgraded to an unsigned claim. Receivers still verify the
    signature and timestamp through add_peer_instance, just like IntroducePeer.

    Walked in a fresh random order each call: a fixed order would relay the same
    first ``limit`` peers every time, and nobody past the cap would ever propagate.
    """
    if limit <= 0:
        return
    emitted = 0
    now = time.time()
    ids = list(sc.get_peers_id())
    for peer_id in random.sample(ids, len(ids)):
        if peer_id == exclude_peer_id:
            continue
        try:
            blob = sc.get_peer_advertisement(peer_id)
            if not blob:
                continue
            peer = celaut_pb2.Peer.FromString(blob)
            if not relayable_peer(peer, peer_id, now):
                continue
        except Exception as exc:
            LOGGER(f"[gossip] could not read advertisement for {peer_id}: {exc}")
            continue
        yield peer
        emitted += 1
        if emitted >= limit:
            return

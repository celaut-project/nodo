"""The price offer that a node signs and keeps until ``valid_until`` (#657).

``EstimatedCost`` is the offer of a node for a start. With ``valid_until`` and a
``signature`` it is also a commitment: the node keeps the price of the offer for a start
that arrives before that time and carries the offer (``Configuration.offer``). The quote in
the ``start`` movement of the instance then has the terms of the offer, and a caller, or
a third party with the offer and the signed statement, checks it with no tolerance.

The price of a node is its load: the offer carries it (``launch_scarcity``, in parts per
million), and the node prices the instance at that load when it keeps the offer
(``offer_honor``). A load is quantised to the million (``quantize``) before the figures of
the offer are computed from it, so the offer and the start read the same price.

No database here, as in ``statement_signature``: a reader of signed evidence checks an
offer of any node with the offer alone. The signature covers a canonical text of every
field, never ``SerializeToString()``: protobuf serialization is not canonical.
"""
import hashlib
import struct
import time
from typing import Dict, Mapping, Optional

from protos import celaut_pb2
from src.identity.node_identity import (
    get_node_public_key_hex, offer_payload, sign_peer_payload, verify_peer_payload,
)

#: Parts per million: the resolution of the load in an offer.
SCARCITY_SCALE = 1_000_000


def quantize(scarcity: Mapping[str, float]) -> Dict[str, int]:
    """The load as whole parts per million, each from 0 to ``SCARCITY_SCALE``."""
    return {
        str(key): max(0, min(SCARCITY_SCALE, int(round(float(value) * SCARCITY_SCALE))))
        for key, value in scarcity.items()
    }


def to_scarcity(ppm: Mapping[str, int]) -> Dict[str, float]:
    """The load of ``quantize``, as the fractions that the pricing functions read."""
    return {key: int(value) / SCARCITY_SCALE for key, value in ppm.items()}


def scarcity_of(offer: celaut_pb2.EstimatedCost) -> Dict[str, int]:
    """The load in ``offer`` in parts per million (the last entry of a key wins)."""
    return {entry.key: int(entry.value) for entry in offer.launch_scarcity}


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def offer_digest(offer: celaut_pb2.EstimatedCost) -> str:
    """Blake2b-256 hex of the canonical text of every field but the signature.

    The text is ``hex(cost.n)|hex(init_maintenance_cost.n)|hex(max_maintenance_cost.n)|
    maintenance_seconds_loop|price_bounded|price_volatility|volatility_period_seconds|
    closes_at|on_close|valid_until|launch_scarcity``. ``price_bounded`` is 1 or 0.
    ``price_volatility`` is the 4 bytes of the float, big-endian, in hex. ``on_close`` is
    the number of the enum. ``launch_scarcity`` is ``key=value`` for each key, sorted by
    key and joined with ``;``.
    """
    scarcity = ";".join(f"{key}={value}" for key, value in sorted(scarcity_of(offer).items()))
    text = "|".join((
        _hex(offer.cost.n), _hex(offer.init_maintenance_cost.n),
        _hex(offer.max_maintenance_cost.n), str(offer.maintenance_seconds_loop),
        "1" if offer.price_bounded else "0",
        struct.pack(">f", offer.price_volatility).hex(),
        str(offer.volatility_period_seconds), str(offer.closes_at), str(int(offer.on_close)),
        str(offer.valid_until), scarcity,
    ))
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def seal(offer: celaut_pb2.EstimatedCost, valid_until: int) -> bool:
    """Sign ``offer`` in place with the key of this node, valid until ``valid_until``.

    Returns False, and leaves the offer with no promise (``valid_until`` 0, no load and no
    signature), when ``valid_until`` is not a time or the node has no identity key. The
    offer then is the one of a node that makes no promise for the start."""
    offer.ClearField("signature")
    public_key = get_node_public_key_hex()
    if int(valid_until) > 0 and public_key and offer.launch_scarcity:
        offer.valid_until = int(valid_until)
        signature = sign_peer_payload(offer_payload(public_key, offer_digest(offer)))
        if signature:
            offer.signature = signature
            return True
    offer.valid_until = 0
    offer.ClearField("launch_scarcity")
    return False


def verify(offer: celaut_pb2.EstimatedCost, peer_id: str) -> bool:
    """Whether ``peer_id`` signed exactly this offer."""
    return bool(offer.signature) and verify_peer_payload(
        peer_id, offer_payload(peer_id, offer_digest(offer)), offer.signature,
    )


def committed_until(offer: Optional[celaut_pb2.EstimatedCost], peer_id: str) -> int:
    """The time until which ``peer_id`` keeps the price of ``offer``, or 0.

    0 when the offer promises nothing, and also when it names a time with no valid
    signature: the caller reads that as no promise."""
    if offer is None or int(offer.valid_until) <= 0 or not verify(offer, peer_id):
        return 0
    return int(offer.valid_until)


def is_current(offer: celaut_pb2.EstimatedCost, now: Optional[float] = None) -> bool:
    """Whether the time of ``offer`` is not past."""
    return int(offer.valid_until) > 0 and (time.time() if now is None else now) <= offer.valid_until

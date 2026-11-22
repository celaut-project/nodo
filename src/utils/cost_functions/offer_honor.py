"""A node keeps the price of its own offer for a start (#657).

The offer of a node (``generate_estimated_cost``) is signed with a time (``valid_until``)
when it leaves the node (``sign``). A caller that starts the service before that time
sends the offer back in ``Configuration.offer``. The node then prices the instance at the
load of the offer, and not at its load of now (``honored``): the rows of the instance, the
ticks that charge it and the quote in its ``start`` movement hold the price that the node
offered. The caller checks that quote with no tolerance (``delegate_execution.stricter_quote``).

The node keeps no state. The offer proves itself: the node checks its own signature, the
time, and that the figures of the offer are the ones that it computes for this service and
these resources at the load of the offer. A caller that sends the offer of a small service
to start a large one fails that last check, and the start is priced at the price of now.
"""
import time
from typing import Mapping, Optional, Tuple

from protos import celaut_pb2
from src.identity.node_identity import get_node_public_key_hex
from src.utils import logger as log
from src.utils.config import ConfigManager
from src.utils.cost_functions import price_bound, signed_offer
from src.utils.cost_functions.execution_cost import RESOURCES

#: ``network.OFFER_VALIDITY_SECONDS`` when the operator sets none.
DEFAULT_VALIDITY_SECONDS = 60

def _figures(offer: celaut_pb2.EstimatedCost) -> Tuple[int, int, int]:
    """What an offer holds to the price: one period at the start resources, at the most,
    and the length of the period."""
    return (int(offer.init_maintenance_cost.n or 0), int(offer.max_maintenance_cost.n or 0),
            int(offer.maintenance_seconds_loop))


def validity_seconds() -> int:
    """``network.OFFER_VALIDITY_SECONDS``: how long a node keeps the price of an offer. 0
    (or anything unreadable or negative) makes no promise: the offer has no signature."""
    try:
        value = int(ConfigManager().get("network.OFFER_VALIDITY_SECONDS", DEFAULT_VALIDITY_SECONDS))
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def sign(offer: celaut_pb2.EstimatedCost, now: Optional[float] = None) -> celaut_pb2.EstimatedCost:
    """A copy of ``offer`` that this node signs to keep its price for ``validity_seconds``.
    With no validity or no identity key, the copy promises nothing."""
    signed = celaut_pb2.EstimatedCost()
    signed.CopyFrom(offer)
    seconds = validity_seconds()
    until = int((time.time() if now is None else now) + seconds) if seconds > 0 else 0
    signed_offer.seal(signed, until)
    return signed


def honored(offer: Optional[celaut_pb2.EstimatedCost], *, metadata, config, resources,
            arch: Optional[str], service, now: Optional[float] = None
            ) -> Optional[Tuple[Mapping[str, float], Optional[price_bound.PriceBound]]]:
    """``(scarcity, bound)`` that a start prices at, from ``offer``, or None.

    None when there is no offer, and when the offer is not valid for this start: it is not
    signed by this node, its time is past, or its figures are not those of this service
    and these resources at its load. The start is then priced at the price of now. The
    reason of a refusal is logged.
    """
    if offer is None or int(offer.valid_until) <= 0:
        return None
    try:
        own_key = get_node_public_key_hex()
        if not own_key or not signed_offer.verify(offer, own_key):
            log.LOGGER("Offer of the start ignored: this node did not sign it.")
            return None
        if not signed_offer.is_current(offer, now):
            log.LOGGER("Offer of the start ignored: its time is past.")
            return None
        load = signed_offer.scarcity_of(offer)
        if any(key not in load for key in RESOURCES):
            log.LOGGER("Offer of the start ignored: it has no load for each resource.")
            return None
        scarcity = signed_offer.to_scarcity(load)
        bound = price_bound.of_quote(offer, source=" in the offer of the start")
        # Imported here: it pulls in the virtualizer stack.
        from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
        replay = generate_estimated_cost(
            metadata=metadata, config=config, resources=resources, arch=arch, service=service,
            check_availability=False, scarcity=scarcity, bound=bound,
        )
        if replay is None or _figures(replay) != _figures(offer):
            log.LOGGER("Offer of the start ignored: it is not the price of this service "
                       "and these resources.")
            return None
        return scarcity, bound
    except Exception as e:
        log.LOGGER(f"Offer of the start ignored: {e}")
        return None

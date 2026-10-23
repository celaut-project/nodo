"""The children that a peer started for a start that timed out here (#578).

``delegate_execution`` waits ``START_SERVICE_ON_PEER_TIMEOUT`` for the peer. A peer can
finish the start after that, and charge our account for a child that nobody holds: the
father already got an error and his MU back. The child would run on the MU that this
node sent until they ran out.

Each start goes with a reference of this node (``start_ref``), and ``StartService`` with
the same reference returns the instance that the first request created, with no new
charge. This node asks for it and stops the child. The peer then writes what the child
did not use as a ``refund`` of its ``child_start``.
"""
from protos import celaut_pb2
from src.database.sql_connection import SQLConnection
from src.identity.grpc_transport import peer_channel
from src.utils import logger as log, start_ref
from src.utils.bee_client import BeeClient


def ask_again(peer_id: str, client_id: str, reference: str, timeout: float):
    """The ServiceInstance of the start ``reference`` (hex) on ``peer_id``. Raises what
    the peer answers: ABORTED while the start runs, FAILED_PRECONDITION when it failed."""
    return BeeClient.start_service(
        peer_channel(peer_id),
        iter([celaut_pb2.Client(client_id=client_id),
              celaut_pb2.StartReference(ref=bytes.fromhex(reference))]),
        timeout=timeout,
    )


def stop_orphan(peer_id: str, client_id: str, reference: str, timeout: float,
                books=None) -> bool:
    """Get the child of the start ``reference`` on ``peer_id`` and stop it. True when it
    is stopped. Never raises: a start that is not stopped is asked for again at the next
    check of our account. ``books`` is a ``SQLConnection``."""
    try:
        instance = ask_again(peer_id, client_id, reference, timeout)
        if instance is None or not instance.token:
            return False
        BeeClient.stop_service(peer_channel(peer_id), token=instance.token)
    except Exception as e:
        log.LOGGER(f"The child of the start {reference[:16]} on {peer_id} is not stopped: {e}")
        return False
    try:
        (books or SQLConnection()).settle_orphan_start(peer_id, reference, instance.token)
    except Exception as e:
        log.LOGGER(f"Could not close the start {reference[:16]} on {peer_id}: {e}")
    log.LOGGER(f"Stopped the child of the start {reference[:16]} on {peer_id}: it timed out here.")
    return True


def stop_orphans(peer_id: str, client_id: str, timeout: float, books=None) -> int:
    """``stop_orphan`` for each start on ``peer_id`` that the peer charged and whose child
    this node never got. A start that ``delegate_execution`` still waits for is not an
    orphan. Returns how many children were stopped."""
    books = books or SQLConnection()
    return sum(stop_orphan(peer_id, client_id, reference, timeout, books)
               for reference in books.orphan_starts(peer_id)
               if not start_ref.in_flight(reference))

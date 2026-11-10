"""How `nodo estimate`, `nodo execute` and `nodo instances` show closing time (issue #344).

A quote says what the node does when its activity window closes (`on_close`) and when
(`closes_at`). Neither is a promise of a long life: the node can stop for a cause that is
not in its schedule, and a `closes_at` of 0 only means that no close is scheduled. The
text says so.

The time is written in the local time of the machine that runs the command, with the time
left: "2026-11-10 18:00 (in 2 h 10 min)".
"""
import calendar
import time
from datetime import datetime
from typing import List, Optional

from src.utils import on_close as on_close_names

# What a node does, in a sentence that an operator or a client can read.
ON_CLOSE_TEXT = {
    "refuse": "it leaves the instance running and refuses new work and growth",
    "stop": "it stops the instance and refunds the balance",
    "hibernate": "it hibernates the instance (the disk is kept) and wakes it when the window opens",
}

NOT_A_PROMISE = ("This is not a promise of a long life: the node can stop for a cause that is "
                 "not in its schedule.")

# The clock of the peer can differ a little from ours: a close that is this much before
# the announced one is not a broken commitment.
BROKEN_COMMITMENT_SKEW_SECONDS = 120


def time_left(seconds: int) -> str:
    """"in 2 h 10 min", "in 3 min", "in 40 s", or "already passed"."""
    if seconds <= 0:
        return "already passed"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"in {hours} h {minutes} min" if minutes else f"in {hours} h"
    if minutes:
        return f"in {minutes} min"
    return f"in {secs} s"


def describe_closes_at(closes_at: Optional[int], now: Optional[float] = None) -> str:
    """The local time of the close and the time left. 0 or None: no close is scheduled."""
    if not closes_at or int(closes_at) <= 0:
        return "no close is scheduled"
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(int(closes_at))
    return f"{local:%Y-%m-%d %H:%M} ({time_left(int(closes_at - now))})"


def closing_lines(on_close: Optional[str], closes_at: Optional[int],
                  now: Optional[float] = None) -> List[str]:
    """The lines that show what a node does at closing time."""
    name = on_close if on_close in ON_CLOSE_TEXT else "refuse"
    return [
        "Closing time:",
        f"- Next close: {describe_closes_at(closes_at, now)}.",
        f"- At the close: {name} -- {ON_CLOSE_TEXT[name]}.",
        f"- {NOT_A_PROMISE}",
    ]


def quote_closing_lines(estimated_cost, now: Optional[float] = None) -> List[str]:
    """The closing lines of a quote (`EstimatedCost.on_close`, `closes_at`)."""
    return closing_lines(on_close_names.to_name(estimated_cost.on_close),
                         int(estimated_cost.closes_at or 0), now)


def instance_closing_text(on_close: Optional[str], closes_at: Optional[int],
                          now: Optional[float] = None) -> str:
    """One short cell for the instance list: the action and the close it was quoted. "-" if
    the row has no value."""
    if on_close is None and closes_at is None:
        return "-"
    name = on_close or "refuse"
    if not closes_at:
        return name
    return f"{name}, {describe_closes_at(closes_at, now)}"


def _epoch(utc_text) -> Optional[int]:
    """The unix time of a SQLite ``CURRENT_TIMESTAMP`` text (UTC), or None."""
    try:
        return calendar.timegm(time.strptime(str(utc_text)[:19], "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None


def broken_commitment(suspended_at, suspended_reason: Optional[str],
                      closes_at: Optional[int]) -> bool:
    """Whether the node closed this instance before the close that its quote announced.

    True for an instance that the window put down (`hibernated_by_window`) earlier than
    `closes_at`, less a margin for the difference between clocks. The node logs the same
    fact when it happens and never blocks the operator.
    """
    if suspended_reason != "hibernated_by_window" or not closes_at:
        return False
    stopped = _epoch(suspended_at)
    return stopped is not None and stopped < int(closes_at) - BROKEN_COMMITMENT_SKEW_SECONDS


def state_label(suspended_at, suspended_reason: Optional[str]) -> str:
    """The state of a local instance, as `nodo instances` writes it."""
    if not suspended_at:
        return "running"
    return {
        "hibernated": "hibernated",
        "hibernated_by_window": "hibernated (activity window)",
        "hibernated_out_of_balance": "hibernated (out of balance)",
    }.get(suspended_reason or "", f"suspended ({suspended_reason})" if suspended_reason else "suspended")


QUOTE_TIMEOUT_SECONDS = 5
# Balance of the throwaway client that only authenticates the question. It is never spent:
# asking a price launches nothing.
QUOTE_CLIENT_BALANCE_MU = 10 ** 16


def quote_before_launch(service_hash: str, timeout: int = QUOTE_TIMEOUT_SECONDS):
    """The quote of this node for ``service_hash``, or None.

    Best effort: `nodo execute` shows it before the launch, and a quote that cannot be
    read (the node refuses, is slow, or has no price) never stops the launch. The lines
    that follow say what the node does at closing time, as `nodo estimate` does.
    """
    try:
        from protos import celaut_pb2
        from src.identity.grpc_transport import local_channel
        from src.manager.manager import get_dev_clients
        from src.utils.bee_client import BeeClient
        from src.utils.utils import read_metadata_from_disk, service_extended

        metadata = read_metadata_from_disk(service_hash=service_hash)
        if not metadata:
            return None
        client_id = next(get_dev_clients(amount_mu=QUOTE_CLIENT_BALANCE_MU))
        channel = local_channel()
        try:
            return BeeClient.get_service_estimated_cost(
                channel,
                service_extended(metadata=metadata, config=celaut_pb2.Configuration(),
                                 send_only_hashes=True, client_id=client_id),
                timeout=timeout,
            ) or None
        finally:
            channel.close()
    except Exception:
        return None


def print_closing_before_launch(service_hash: str) -> None:
    """Print what the node does at closing time, before ``nodo execute`` launches."""
    quote = quote_before_launch(service_hash)
    if quote is None:
        return
    for line in quote_closing_lines(quote):
        print(line)
    print()

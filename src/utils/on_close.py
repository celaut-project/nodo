"""The names of `ON_CLOSE` and the `OnClose` enum of the protocol (issue #344).

The configuration, the database rows and the log use the names ("refuse", "stop",
"hibernate"). The quote and the `Peer` announcement use the enum. This module is the one
place that converts between the two.
"""
from protos import celaut_pb2

_TO_ENUM = {
    "refuse": celaut_pb2.ON_CLOSE_REFUSE,
    "stop": celaut_pb2.ON_CLOSE_STOP,
    "hibernate": celaut_pb2.ON_CLOSE_HIBERNATE,
}
_TO_NAME = {value: name for name, value in _TO_ENUM.items()}


def to_enum(name) -> int:
    """The enum value of ``name``. A name that nobody recognises is `ON_CLOSE_REFUSE`."""
    return _TO_ENUM.get(str(name or "").strip().lower(), celaut_pb2.ON_CLOSE_REFUSE)


def to_name(value) -> str:
    """The name of an enum value. A value that this node does not know is "refuse": the
    default of the enum, and what an older node reads."""
    return _TO_NAME.get(int(value or 0), "refuse")

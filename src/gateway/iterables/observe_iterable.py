from typing import Any, Dict, Generator

from bee_rpc import client as bee, buffer_pb2

from protos import celaut_pb2
from src.commands.observe import observe_event_stream, ObserveInstanceError
from src.utils.logger import LOGGER as logger


def _event_to_proto(event: Dict[str, Any]) -> celaut_pb2.ObserveEvent:
    """Convert one :func:`observe_event_stream` dict into an ``ObserveEvent``.

    The dict's ``kind`` selects which oneof payload is populated. Optional proto
    fields are only set when the source value is present, so unset stays unset on
    the wire (mirroring the ``None`` fields in ``metrics.jsonl``).
    """
    kind = event.get("kind", "")
    proto = celaut_pb2.ObserveEvent(kind=kind, time=event.get("time", ""))

    if kind == "session":
        proto.session.instance_id = event.get("instance_id", "")
        proto.session.tag = event.get("tag") or ""
        proto.session.capture_mode = event.get("capture_mode", "")
        proto.session.degraded_reason = event.get("degraded_reason") or ""
        if event.get("link_type") is not None:
            proto.session.link_type = event["link_type"]
        if event.get("snaplen") is not None:
            proto.session.snaplen = event["snaplen"]
        proto.session.gas = event.get("gas") or ""

    elif kind == "metrics":
        m = proto.metrics
        m.alive = bool(event.get("alive"))
        if event.get("cpu_percent") is not None:
            m.cpu_percent = event["cpu_percent"]
        if event.get("cpu_peak_percent") is not None:
            m.cpu_peak_percent = event["cpu_peak_percent"]
        if event.get("mem_bytes") is not None:
            m.mem_bytes = event["mem_bytes"]
        if event.get("mem_peak_bytes") is not None:
            m.mem_peak_bytes = event["mem_peak_bytes"]
        if event.get("disk_read_bytes") is not None:
            m.disk_read_bytes = event["disk_read_bytes"]
        if event.get("disk_write_bytes") is not None:
            m.disk_write_bytes = event["disk_write_bytes"]
        if event.get("net_rx_bytes") is not None:
            m.net_rx_bytes = event["net_rx_bytes"]
        if event.get("net_tx_bytes") is not None:
            m.net_tx_bytes = event["net_tx_bytes"]
        if event.get("net_rx_packets") is not None:
            m.net_rx_packets = event["net_rx_packets"]
        if event.get("net_tx_packets") is not None:
            m.net_tx_packets = event["net_tx_packets"]
        if event.get("gas") is not None:
            m.gas = event["gas"]

    elif kind == "packet":
        record = event.get("record", {})
        p = proto.packet
        p.direction = record.get("direction") or ""
        p.transport = record.get("transport") or ""
        p.protocol = record.get("protocol") or ""
        p.tcp_flags = record.get("tcp_flags") or ""
        p.src = record.get("src") or ""
        p.dst = record.get("dst") or ""
        if event.get("frame_len") is not None:
            p.frame_len = event["frame_len"]
        if event.get("frame") is not None:
            p.raw_frame = event["frame"]
            if event.get("frame_ts") is not None:
                p.frame_timestamp = event["frame_ts"]
        p.peer_kind = record.get("peer_kind") or ""
        p.peer_instance_id = record.get("peer_instance_id") or ""
        p.peer_tag = record.get("peer_tag") or ""
        p.peer_relationship = record.get("peer_relationship") or ""
        p.peer_host = record.get("peer_host") or ""
        p.source = event.get("source") or ""

    elif kind == "notice":
        proto.notice.message = event.get("message", "")
        proto.notice.degraded = bool(event.get("degraded"))

    return proto


class ObserveIterable:
    """Stream a running instance's live observability data over the gateway.

    Input : one ``ObserveRequest`` (instance id/token + ``include_packets``).
    Output: a live stream of ``ObserveEvent`` — a ``session`` event, then
            ``metrics`` snapshots, ``packet`` records and ``notice`` messages —
            sourced from the same :func:`observe_event_stream` core the
            ``nodo observe`` CLI consumes. The instance is addressed exactly like
            ``GetMetrics`` (``TokenMessage.token`` semantics).

    The stream ends cleanly when the instance stops, the capture socket dies, or
    the client cancels (``context.is_active()`` goes false — the AF_PACKET socket
    is released in ``observe_event_stream``'s ``finally``). Instance-not-found /
    not-running is surfaced as a trailing degraded ``notice`` rather than a hard
    gRPC error, so a client always gets a structured reason.
    """

    def __init__(self, request_iterator, context):
        self.request_iterator = request_iterator
        self.context = context

    def _should_stop(self) -> bool:
        """True once the client has cancelled / disconnected."""
        is_active = getattr(self.context, "is_active", None)
        if callable(is_active):
            try:
                return not is_active()
            except Exception:  # pragma: no cover - defensive
                return False
        return False

    def _events(self) -> Generator[celaut_pb2.ObserveEvent, None, None]:
        request = next(bee.parse_from_buffer(
            request_iterator=self.request_iterator,
            indices=celaut_pb2.ObserveRequest,
            partitions_message_mode=True
        ), None)

        if request is None or not request.instance_id:
            raise Exception("Observe: missing instance_id in ObserveRequest.")

        instance_id = request.instance_id
        include_packets = bool(request.include_packets)
        logger(f'Observe request for instance {instance_id} '
               f'(include_packets={include_packets}).')

        try:
            for event in observe_event_stream(
                    instance_id,
                    include_packets=include_packets,
                    should_stop=self._should_stop,
            ):
                yield _event_to_proto(event)
        except (ObserveInstanceError, ValueError) as exc:
            # Not found / not running / ambiguous id: end with a structured
            # notice instead of aborting, so the client learns why.
            message = getattr(exc, "message", str(exc))
            logger(f'Observe: {message}')
            yield celaut_pb2.ObserveEvent(
                kind="notice",
                notice=celaut_pb2.ObserveEvent.Notice(message=message, degraded=True),
            )
        finally:
            logger(f'Observe stream for {instance_id} finished.')

    def __iter__(self) -> Generator[buffer_pb2.Buffer, None, None]:
        yield from bee.serialize_to_buffer(
            message_iterator=self._events(),
            indices=celaut_pb2.ObserveEvent,
        )

"""Unit tests for the ``Gateway.Observe`` streaming RPC and its shared core.

Two layers are covered without a live VM or root:

* :func:`src.commands.observe.observe_event_stream` — the refactored capture /
  metrics generator both the CLI and the RPC consume — driven in conntrack mode
  with the runtime touchpoints mocked (no AF_PACKET socket needed).
* :mod:`src.gateway.iterables.observe_iterable` — request parsing, event → proto
  conversion, and the live ``bee_rpc`` serialization, driven with a synthetic
  event source and a fake gRPC context.

Full AF_PACKET tap capture is exercised on the Linux/KVM host, not here.
"""

import itertools
import unittest
from unittest import mock

from src.commands import observe


def _drive_conntrack_stream(should_stop, *, include_packets=False,
                            conntrack_events=None, alive_samples=None):
    """Run ``observe_event_stream`` in conntrack mode with the runtime mocked.

    Forcing ``resolve_capture_source`` to return no tap keeps us off AF_PACKET
    (no raw socket / select), so the loop is fully deterministic. ``should_stop``
    controls how many ticks run; ``REFRESH_INTERVAL_S`` is zeroed so there are no
    real sleeps.
    """
    if conntrack_events is None:
        conntrack_events = [{
            "direction": "OUT", "transport": "tcp", "protocol": "TCP",
            "src_ip": "10.0.0.5", "dst_ip": "1.2.3.4",
            "src_port": 5000, "dst_port": 443, "tcp_flags": None,
            "peer_ip": "1.2.3.4",
        }]
    usage = itertools.count(1_000_000, 1_000_000)
    mem = itertools.count(4096, 1024)

    def _sample(_id):
        return {"alive": True, "mem_bytes": next(mem), "cpu_usage_usec": next(usage)}

    with mock.patch.object(observe, "REFRESH_INTERVAL_S", 0.0), \
            mock.patch.object(observe, "resolve_instance",
                              return_value={"id": "inst-full-123", "ip": "10.0.0.5",
                                            "father_id": "", "service_id": ""}), \
            mock.patch.object(observe, "_sample_resources", side_effect=_sample), \
            mock.patch.object(observe, "resolve_capture_source",
                              return_value=(None, "AF_PACKET unavailable (test)")), \
            mock.patch.object(observe, "read_conntrack_events",
                              return_value=(conntrack_events, None)), \
            mock.patch.object(observe, "build_instance_index", return_value={}), \
            mock.patch.object(observe, "resolve_tag", return_value=None):
        return list(observe.observe_event_stream(
            "inst", include_packets=include_packets, should_stop=should_stop))


class ObserveEventStreamTests(unittest.TestCase):
    def _stop_after(self, ticks):
        state = {"n": 0}

        def should_stop():
            state["n"] += 1
            return state["n"] > ticks
        return should_stop

    def test_session_event_is_first_and_describes_capture(self):
        events = _drive_conntrack_stream(self._stop_after(1))
        self.assertEqual(events[0]["kind"], "session")
        session = events[0]
        self.assertEqual(session["instance_id"], "inst-full-123")
        self.assertEqual(session["capture_mode"], "conntrack")
        self.assertIn("AF_PACKET unavailable", session["degraded_reason"])

    def test_stream_yields_metrics_and_packet_events(self):
        events = _drive_conntrack_stream(self._stop_after(2))
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds[0], "session")
        self.assertIn("metrics", kinds)
        self.assertIn("packet", kinds)

        packet = next(e for e in events if e["kind"] == "packet")
        # The parsed flow is exposed both flattened (record) and raw (for the
        # CLI's per-flow table), with a classified peer.
        self.assertEqual(packet["record"]["src"], "10.0.0.5:5000")
        self.assertEqual(packet["record"]["dst"], "1.2.3.4:443")
        self.assertEqual(packet["record"]["peer_kind"], "external")
        self.assertEqual(packet["source"], "conntrack")
        self.assertEqual(packet["raw"]["peer"]["host"], "1.2.3.4")

        metrics = next(e for e in events if e["kind"] == "metrics")
        self.assertTrue(metrics["alive"])
        self.assertIn("mem_bytes", metrics)
        self.assertIn("cpu_peak_percent", metrics)

    def test_include_packets_false_drops_raw_frame_bytes(self):
        events = _drive_conntrack_stream(self._stop_after(1), include_packets=False)
        packet = next(e for e in events if e["kind"] == "packet")
        # Conntrack has no frame anyway, but the contract is: frame is None when
        # packets aren't opted in. Byte counts are unavailable → frame_len None.
        self.assertIsNone(packet["frame"])
        self.assertIsNone(packet["frame_len"])

    def test_instance_not_found_raises(self):
        with mock.patch.object(observe, "resolve_instance", return_value=None):
            with self.assertRaises(observe.ObserveInstanceError) as ctx:
                list(observe.observe_event_stream("missing"))
        self.assertIn("No local instance", ctx.exception.message)

    def test_instance_not_running_raises(self):
        with mock.patch.object(observe, "resolve_instance",
                               return_value={"id": "x", "ip": "", "father_id": "",
                                             "service_id": ""}), \
                mock.patch.object(observe, "_sample_resources",
                                  return_value={"alive": False}):
            with self.assertRaises(observe.ObserveInstanceError) as ctx:
                list(observe.observe_event_stream("x"))
        self.assertIn("not running", ctx.exception.message)

    def test_attach_failure_raises_with_reason(self):
        with mock.patch.object(observe, "resolve_instance",
                               return_value={"id": "x", "ip": "", "father_id": "",
                                             "service_id": ""}), \
                mock.patch.object(observe, "_sample_resources",
                                  side_effect=RuntimeError("boom")):
            with self.assertRaises(observe.ObserveInstanceError) as ctx:
                list(observe.observe_event_stream("x"))
        self.assertIn("Unable to attach", ctx.exception.message)


# The RPC glue imports bee_rpc + the node logger; keep the import local so the
# pure-core tests above still run in environments without them.
from src.gateway.iterables import observe_iterable as OI  # noqa: E402
from bee_rpc import client as bee  # noqa: E402
from protos import celaut_pb2  # noqa: E402


class EventToProtoTests(unittest.TestCase):
    def test_session_conversion(self):
        proto = OI._event_to_proto({
            "kind": "session", "time": "12:00:00", "instance_id": "inst-1",
            "tag": "gateway", "capture_mode": "pcap", "degraded_reason": None,
        })
        self.assertEqual(proto.kind, "session")
        self.assertEqual(proto.WhichOneof("payload"), "session")
        self.assertEqual(proto.session.instance_id, "inst-1")
        self.assertEqual(proto.session.capture_mode, "pcap")
        self.assertEqual(proto.session.degraded_reason, "")  # None → empty string.

    def test_metrics_conversion_sets_present_fields_only(self):
        proto = OI._event_to_proto({
            "kind": "metrics", "time": "12:00:01", "alive": True,
            "cpu_percent": 12.5, "cpu_peak_percent": None,
            "mem_bytes": 4096, "mem_peak_bytes": None,
        })
        self.assertEqual(proto.WhichOneof("payload"), "metrics")
        self.assertTrue(proto.metrics.alive)
        self.assertAlmostEqual(proto.metrics.cpu_percent, 12.5)
        self.assertEqual(proto.metrics.mem_bytes, 4096)
        # None-valued optionals stay unset on the wire.
        self.assertFalse(proto.metrics.HasField("cpu_peak_percent"))
        self.assertFalse(proto.metrics.HasField("mem_peak_bytes"))

    def test_packet_conversion(self):
        proto = OI._event_to_proto({
            "kind": "packet", "time": "12:00:01", "frame_len": 74, "source": "pcap",
            "record": {
                "direction": "IN", "transport": "udp", "protocol": "UDP",
                "tcp_flags": None, "src": "1.2.3.4:53", "dst": "10.0.0.5:40000",
                "peer_kind": "instance", "peer_instance_id": "peer-9",
                "peer_tag": "dns", "peer_relationship": "child",
            },
        })
        self.assertEqual(proto.WhichOneof("payload"), "packet")
        self.assertEqual(proto.packet.direction, "IN")
        self.assertEqual(proto.packet.transport, "udp")
        self.assertEqual(proto.packet.src, "1.2.3.4:53")
        self.assertEqual(proto.packet.frame_len, 74)
        self.assertEqual(proto.packet.peer_kind, "instance")
        self.assertEqual(proto.packet.peer_relationship, "child")
        self.assertEqual(proto.packet.source, "pcap")

    def test_notice_conversion(self):
        proto = OI._event_to_proto({
            "kind": "notice", "time": "12:00:02",
            "message": "instance stopped", "degraded": False,
        })
        self.assertEqual(proto.WhichOneof("payload"), "notice")
        self.assertEqual(proto.notice.message, "instance stopped")
        self.assertFalse(proto.notice.degraded)


class _FakeContext:
    def __init__(self, active=True):
        self._active = active

    def is_active(self):
        return self._active


class ObserveIterableTests(unittest.TestCase):
    def _request_buffers(self, instance_id="inst-123", include_packets=True):
        return list(bee.serialize_to_buffer(
            message_iterator=celaut_pb2.ObserveRequest(
                instance_id=instance_id, include_packets=include_packets),
            indices=celaut_pb2.ObserveRequest,
        ))

    def _collect(self, request_buffers, context):
        out = list(OI.ObserveIterable(iter(request_buffers), context))
        return list(bee.parse_from_buffer(
            request_iterator=iter(out),
            indices=celaut_pb2.ObserveEvent,
            partitions_message_mode=True,
        ))

    def test_request_parsed_and_events_streamed(self):
        fake_events = [
            {"kind": "session", "time": "12:00:00", "instance_id": "inst-123",
             "tag": "gateway", "capture_mode": "conntrack",
             "degraded_reason": "AF_PACKET unavailable"},
            {"kind": "metrics", "time": "12:00:01", "alive": True,
             "cpu_percent": 12.5, "cpu_peak_percent": 30.0,
             "mem_bytes": 4096, "mem_peak_bytes": 8192},
            {"kind": "packet", "time": "12:00:01", "frame_len": 74,
             "source": "conntrack",
             "record": {"direction": "OUT", "transport": "tcp", "protocol": "TCP",
                        "tcp_flags": "SYN", "src": "10.0.0.5:5000",
                        "dst": "1.2.3.4:443", "peer_kind": "external",
                        "peer_host": "1.2.3.4"}},
        ]
        seen = {}

        def fake_stream(instance_id, *, include_packets, should_stop):
            seen["instance_id"] = instance_id
            seen["include_packets"] = include_packets
            yield from fake_events

        with mock.patch.object(OI, "observe_event_stream", fake_stream):
            events = self._collect(self._request_buffers(), _FakeContext())

        self.assertEqual(seen["instance_id"], "inst-123")
        self.assertTrue(seen["include_packets"])
        self.assertEqual([e.kind for e in events], ["session", "metrics", "packet"])
        self.assertAlmostEqual(events[1].metrics.cpu_percent, 12.5)
        self.assertEqual(events[2].packet.dst, "1.2.3.4:443")
        self.assertEqual(events[2].packet.peer_host, "1.2.3.4")

    def test_instance_error_becomes_trailing_notice(self):
        def boom(instance_id, *, include_packets, should_stop):
            raise observe.ObserveInstanceError("Instance abc is not running.")
            yield  # pragma: no cover - makes this a generator.

        with mock.patch.object(OI, "observe_event_stream", boom):
            events = self._collect(self._request_buffers(), _FakeContext())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "notice")
        self.assertTrue(events[0].notice.degraded)
        self.assertIn("not running", events[0].notice.message)

    def test_missing_instance_id_raises(self):
        empty = list(bee.serialize_to_buffer(
            message_iterator=celaut_pb2.ObserveRequest(instance_id=""),
            indices=celaut_pb2.ObserveRequest,
        ))
        with self.assertRaises(Exception) as ctx:
            list(OI.ObserveIterable(iter(empty), _FakeContext()))
        self.assertIn("instance_id", str(ctx.exception))

    def test_should_stop_tracks_context_activity(self):
        active = OI.ObserveIterable(iter([]), _FakeContext(active=True))
        cancelled = OI.ObserveIterable(iter([]), _FakeContext(active=False))
        self.assertFalse(active._should_stop())
        self.assertTrue(cancelled._should_stop())


if __name__ == "__main__":
    unittest.main()

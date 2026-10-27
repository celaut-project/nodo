"""Execution receipts on the wire (#419): StartService, ServiceTunnel and Observe.

The relay runs against a real loopback TCP server, as in ``test_service_tunnel``; only
the instance lookups, the charge and the identity key are patched. What these pin:

* a caller that asks for receipts gets checkpoints and a close whose chains it can
  rebuild from its own messages, all signed by the node;
* a caller that does not ask gets no index 2, and nothing is hashed for it;
* the celaut-gateway layer declares the new formats.
"""
import socket
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from bee_rpc import client as bee
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from protos import celaut_pb2
    from protos.gateway_bee import rpc_input, rpc_output
    from src.identity import execution_receipt as er
    from src.tunneling import rpc_tunnel
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

DEADLINE_S = 5.0
TOKEN = "tok-419"


def _key():
    return ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def _peer_id():
    return _key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


@contextmanager
def _node_identity():
    """This node signs with the vector key."""
    key = _key()
    with mock.patch.object(er, "node_signer", return_value=lambda r: er.sign_with(r, key)), \
            mock.patch.object(er, "node_peer_id", return_value=_peer_id()):
        yield


def _deadline():
    expires = time.monotonic() + DEADLINE_S
    return lambda: time.monotonic() < expires


def _echo_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(DEADLINE_S)
    port = server.getsockname()[1]

    def run():
        try:
            conn, _ = server.accept()
            with conn:
                conn.sendall(b"HELLO")
                while True:
                    data = conn.recv(4096)
                    if not data:
                        return
                    conn.sendall(data.upper())
        except OSError:
            pass
        finally:
            server.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def _instance(port):
    return celaut_pb2.Instance(
        api=celaut_pb2.Service.Api(slot=[celaut_pb2.Service.Api.Slot(
            port=port, transport=celaut_pb2.Service.Api.Protocol(tags=["tcp"]))]),
        uri_slot=[celaut_pb2.Instance.Uri_Slot(
            internal_port=port, uri=[celaut_pb2.Instance.Uri(ip="127.0.0.1", port=port)])],
    ).SerializeToString()




@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TunnelReceiptTests(unittest.TestCase):

    def _run(self, handshake, payload, every_bytes=None):
        port = _echo_server()
        sent = list(payload)

        def requests():
            yield handshake(port)
            for message in sent:
                time.sleep(0.05)
                yield message

        with ExitStack() as stack:
            stack.enter_context(_node_identity())
            stack.enter_context(mock.patch.object(
                rpc_tunnel.sc, "get_internal_instance", return_value=_instance(port)))
            stack.enter_context(mock.patch.object(
                rpc_tunnel.sc, "get_internal_ip", return_value="127.0.0.1"))
            stack.enter_context(mock.patch("src.manager.manager.spend_mu", return_value=True))
            if every_bytes:
                stack.enter_context(mock.patch.object(
                    er, "checkpoint_intervals", return_value=(every_bytes, 1e9)))
            _conn, relay = rpc_tunnel.service_tunnel(requests(), is_active=_deadline())
            return sent, list(relay)

    def test_checkpoints_and_close_match_what_the_client_saw(self):
        sent, out = self._run(
            lambda port: celaut_pb2.TokenMessage(token=TOKEN, slot=str(port), receipts=True),
            [b"ab", b"cd", b"ef"], every_bytes=4,
        )
        got = [m for m in out if isinstance(m, bytes)]
        receipts = [m for m in out if isinstance(m, celaut_pb2.ExecutionReceipt)]
        self.assertEqual(b"".join(got), b"HELLOABCDEF")
        self.assertGreaterEqual(len(receipts), 2)
        self.assertEqual(receipts[-1].kind, er.KIND_CLOSE)
        self.assertTrue(all(r.kind == er.KIND_CHECKPOINT for r in receipts[:-1]))
        self.assertIs(out[-1], receipts[-1])          # the close is the last message

        close = receipts[-1]
        self.assertEqual((close.in_events, close.in_bytes, close.in_head),
                         er.replay_chain(sent, er.DIRECTION_IN))
        self.assertEqual((close.out_events, close.out_bytes, close.out_head),
                         er.replay_chain(got, er.DIRECTION_OUT))
        self.assertEqual(close.coverage, er.COVERAGE_CLIENT_TUNNEL)
        self.assertEqual(close.plane, er.PLANE_TUNNEL)

        report = er.check_sequence(receipts, peer_id=_peer_id(), token=TOKEN)
        self.assertTrue(report["valid"], report)

        # Each checkpoint's heads are those of the messages before it in the stream.
        for index, message in enumerate(out):
            if isinstance(message, celaut_pb2.ExecutionReceipt) and message.kind == er.KIND_CHECKPOINT:
                before = [m for m in out[:index] if isinstance(m, bytes)]
                self.assertEqual(message.out_head,
                                 er.replay_chain(before, er.DIRECTION_OUT)[2])

    def test_a_tampered_checkpoint_fails(self):
        _sent, out = self._run(
            lambda port: celaut_pb2.TokenMessage(token=TOKEN, slot=str(port), receipts=True),
            [b"ab"],
        )
        close = [m for m in out if isinstance(m, celaut_pb2.ExecutionReceipt)][-1]
        self.assertTrue(er.verify(close, token=TOKEN))
        close.out_bytes -= 1
        self.assertFalse(er.verify(close, token=TOKEN))

    def test_a_caller_that_does_not_ask_gets_only_bytes(self):
        with mock.patch.object(er.ReceiptChain, "record") as record:
            _sent, out = self._run(
                lambda port: celaut_pb2.TokenMessage(token=TOKEN, slot=str(port)), [b"ab"],
            )
        self.assertTrue(out)
        self.assertTrue(all(isinstance(m, bytes) for m in out))
        record.assert_not_called()                     # nothing is hashed for it

    def test_a_stream_without_receipts_has_only_index_0(self):
        _sent, out = self._run(
            lambda port: celaut_pb2.TokenMessage(token=TOKEN, slot=str(port)), [b"ab"],
        )
        buffers = list(bee.serialize_to_buffer(message_iterator=iter(out),
                                               indices=rpc_output("ServiceTunnel")))
        parsed = list(bee.parse_from_buffer(request_iterator=iter(buffers), indices={0: bytes},
                                            partitions_message_mode={0: True}))
        self.assertEqual(b"".join(parsed), b"HELLOAB")

    def test_a_new_client_parses_receipts_at_index_2(self):
        _sent, out = self._run(
            lambda port: celaut_pb2.TokenMessage(token=TOKEN, slot=str(port), receipts=True),
            [b"ab"],
        )
        buffers = list(bee.serialize_to_buffer(message_iterator=iter(out),
                                               indices=rpc_output("ServiceTunnel")))
        self.assertIn(2, {b.head.index for b in buffers if b.HasField("head")})
        parsed = list(bee.parse_from_buffer(
            request_iterator=iter(buffers), indices=rpc_output("ServiceTunnel"),
            partitions_message_mode={0: True, 2: True}))
        self.assertEqual(parsed[-1], out[-1])

    def test_no_identity_key_means_no_receipts(self):
        port = _echo_server()
        with mock.patch.object(er, "node_signer", return_value=None), \
                mock.patch.object(rpc_tunnel.sc, "get_internal_instance", return_value=_instance(port)), \
                mock.patch.object(rpc_tunnel.sc, "get_internal_ip", return_value="127.0.0.1"), \
                mock.patch("src.manager.manager.spend_mu", return_value=True):
            _conn, relay = rpc_tunnel.service_tunnel(
                iter([celaut_pb2.TokenMessage(token=TOKEN, slot=str(port), receipts=True), b"x"]),
                is_active=_deadline())
            out = list(relay)
        self.assertTrue(all(isinstance(m, bytes) for m in out))



@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StartReceiptTests(unittest.TestCase):

    def test_the_start_receipt_of_local_execution(self):
        from src.gateway.launcher.local_execution import local_execution as le
        config = celaut_pb2.Configuration()
        entry = config.environment_variables.add()
        entry.key, entry.value = "SOURCE_MNEMONIC", b"secret words"
        with _node_identity():
            receipt = le._start_receipt(token=TOKEN, service_id="ab" * 32,
                                        config=config, initial_mu=1000)
        self.assertTrue(er.verify(receipt, peer_id=_peer_id(), token=TOKEN))
        self.assertEqual(receipt.initial_mu.n, "1000")
        self.assertEqual(bytes(receipt.service_id.value), bytes.fromhex("ab" * 32))
        # The digest is over the raw value, salted; the value is not in the receipt.
        self.assertEqual(receipt.envs_digest, er.envs_digest(
            {"SOURCE_MNEMONIC": b"secret words"}, receipt.envs_salt))
        self.assertNotIn(b"secret words", receipt.SerializeToString())

    def test_no_identity_key_means_no_start_receipt(self):
        from src.gateway.launcher.local_execution import local_execution as le
        with mock.patch.object(er, "node_signer", return_value=None):
            self.assertIsNone(le._start_receipt(token=TOKEN, service_id="ab" * 32,
                                                config=celaut_pb2.Configuration(), initial_mu=1))

    def test_a_receipt_that_cannot_be_built_does_not_fail_the_start(self):
        from src.gateway.launcher.local_execution import local_execution as le
        with _node_identity():
            self.assertIsNone(le._start_receipt(token=TOKEN, service_id="not hex",
                                                config=celaut_pb2.Configuration(), initial_mu=1))

    def test_start_service_carries_the_receipt(self):
        with _node_identity():
            receipt = er.start_receipt(TOKEN, celaut_pb2.Metadata.HashTag.Hash(value=b"s"), {}, 1)
        instance = celaut_pb2.ServiceInstance(token=TOKEN, receipt=receipt)
        buffers = list(bee.serialize_to_buffer(message_iterator=instance,
                                               indices=rpc_output("StartService")))
        parsed = next(bee.parse_from_buffer(request_iterator=iter(buffers),
                                            indices=rpc_output("StartService"),
                                            partitions_message_mode=True))
        self.assertTrue(er.verify(parsed.receipt, token=TOKEN))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ObserveReceiptTests(unittest.TestCase):

    EVENTS = [
        {"kind": "session", "time": "12:00:00", "instance_id": TOKEN, "tag": "t",
         "capture_mode": "pcap", "degraded_reason": ""},
        {"kind": "packet", "time": "12:00:01", "frame_len": 60, "frame": b"\x01" * 60,
         "source": "pcap", "record": {"direction": "IN"}},
        {"kind": "packet", "time": "12:00:01", "frame_len": 1500, "frame": b"\x02" * 64,
         "source": "pcap", "record": {"direction": "OUT"}},
        {"kind": "metrics", "time": "12:00:02", "alive": True},
    ]

    def _events(self, include_receipts, include_packets=True, events=None):
        from src.gateway.iterables import observe_iterable as OI

        def fake_stream(instance_id, *, include_packets, should_stop):
            yield from (events or self.EVENTS)

        request = list(bee.serialize_to_buffer(
            message_iterator=celaut_pb2.ObserveRequest(
                instance_id=TOKEN, include_packets=include_packets,
                include_receipts=include_receipts),
            indices=celaut_pb2.ObserveRequest))

        class _Context:
            def is_active(self):
                return True

        with _node_identity(), mock.patch.object(OI, "observe_event_stream", fake_stream):
            out = list(OI.ObserveIterable(iter(request), _Context()))
        return list(bee.parse_from_buffer(request_iterator=iter(out),
                                          indices=celaut_pb2.ObserveEvent,
                                          partitions_message_mode=True))

    def test_a_close_with_the_capture_mode_signed(self):
        events = self._events(include_receipts=True)
        receipts = [e.receipt for e in events if e.kind == "receipt"]
        self.assertEqual(len(receipts), 1)
        close = receipts[0]
        self.assertEqual(close.kind, er.KIND_CLOSE)
        self.assertEqual((close.plane, close.coverage, close.capture_mode),
                         (er.PLANE_OBSERVE, er.COVERAGE_FULL_TAP, "pcap"))
        self.assertEqual((close.in_events, close.in_bytes), (1, 60))
        self.assertEqual((close.out_events, close.out_bytes), (1, 1500))
        packets = [e.packet for e in events if e.kind == "packet"]
        self.assertEqual(close.out_head, er.replay_chain(
            [(p.frame_len, p.raw_frame) for p in packets if p.direction == "OUT"],
            er.DIRECTION_OUT)[2])
        self.assertTrue(er.verify(close, token=TOKEN))
        close.capture_mode = "conntrack"
        self.assertFalse(er.verify(close, token=TOKEN))

    def test_conntrack_is_counters_only(self):
        events = [dict(self.EVENTS[0], capture_mode="conntrack",
                       degraded_reason="AF_PACKET unavailable")] + self.EVENTS[1:]
        close = [e.receipt for e in self._events(True, events=events) if e.kind == "receipt"][-1]
        self.assertEqual(close.coverage, er.COVERAGE_COUNTERS)
        self.assertEqual(close.degraded_reason, "AF_PACKET unavailable")

    def test_without_packets_it_is_counters_only(self):
        close = [e.receipt for e in self._events(True, include_packets=False)
                 if e.kind == "receipt"][-1]
        self.assertEqual(close.coverage, er.COVERAGE_COUNTERS)

    def test_a_caller_that_does_not_ask_gets_no_receipt_event(self):
        kinds = [e.kind for e in self._events(include_receipts=False)]
        self.assertNotIn("receipt", kinds)
        self.assertEqual(kinds, ["session", "packet", "packet", "metrics"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DeclarationTests(unittest.TestCase):

    def test_the_gateway_layer_declares_the_receipt(self):
        from src.identity import transport_stack
        from src.identity.node_identity import parse_component_formal
        _tags, prose, formal = transport_stack.gateway_component()
        pairs = parse_component_formal(formal)
        self.assertEqual(pairs["receipt.signature.payload"],
                         "celaut-execution-receipt:<public_key_hex>|<receipt_digest>")
        for key in ("receipt.digest", "receipt.execution_name", "receipt.envs_digest",
                    "receipt.chain", "receipt.kinds", "receipt.coverage", "receipt.tunnel"):
            self.assertIn(key, pairs)
        self.assertIn("out:0=bytes,2=celaut.ExecutionReceipt", pairs["rpc.ServiceTunnel"])
        self.assertIn("schema.celaut.ExecutionReceipt", pairs)
        self.assertIn("EXECUTION RECEIPTS.", prose)

    def test_the_digest_text_names_every_signed_field(self):
        """The formal text and the code list the same fields, in the same order."""
        from src.identity import transport_stack
        from src.identity.node_identity import parse_component_formal
        declared = parse_component_formal(transport_stack.gateway_component()[2])["receipt.digest"]
        names = [part.split("(")[-1].split(".")[0].rstrip(")") for part in
                 declared.split("UTF-8 of ", 1)[1].replace("~", "|").split("|")]
        fields = [f.name for f in celaut_pb2.ExecutionReceipt.DESCRIPTOR.fields
                  if f.name not in ("envs_salt", "signature")]
        expected = [n for n in fields if n != "signature_scheme"] + ["scheme"]
        expected.insert(expected.index("service_id") + 1, "service_id")
        self.assertEqual(names, expected)


if __name__ == "__main__":
    unittest.main()

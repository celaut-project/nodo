"""AbstractInputServiceIterable's client_id gate (issue #428) must not depend on
message arrival order.

A Client can legitimately arrive anywhere in this envelope -- client_gate's own
contract says so, and every other gated RPC in this codebase honours it. The base
class used to check for a caller the instant the service looked ready to serve,
which meant a caller that sent its resolving Hash before its Client got refused for
an id it was still in the middle of sending. These pin the fix: the check defers
instead of refusing outright when no client_id has arrived *yet*, and only becomes a
real refusal once the stream actually ends without one ever showing up.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from bee_rpc import client as bee
    from protos import celaut_pb2
    from protos.gateway_bee import StartService_input_indices
    from src.gateway.client_gate import ClientRequired
    import src.gateway.iterables.abstract_input_service_iterable as aisi
    from src.gateway.iterables.abstract_input_service_iterable import AbstractInputServiceIterable
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


class _Context:
    def peer(self) -> str:
        return "ipv4:9.9.9.9:4442"


class _RecordingIterable(AbstractInputServiceIterable):
    def __init__(self, request_iterator, context):
        super().__init__(request_iterator, context)
        self.generate_calls = 0

    def generate(self):
        self.generate_calls += 1
        return
        yield  # pragma: no cover - makes this a generator function, never reached


def _request(messages):
    return iter(list(bee.serialize_to_buffer(
        message_iterator=messages, indices=dict(StartService_input_indices)
    )))


def _fake_require_caller(context, client_id):
    if client_id == "known":
        return client_id
    raise ClientRequired("no usable identity")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ClientOrderIndependenceTests(unittest.TestCase):
    def setUp(self):
        # A hash that "resolves" immediately, as if it were already on the local
        # registry -- the case that flips service_saved on the very first message,
        # before a Client sent afterward would have had any chance to arrive.
        patcher = patch.object(aisi, "find_service_hash", return_value=("known-hash", True))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _hash(self):
        # Metadata, not a bare Hash: it resolves the service the same way (via the
        # mocked find_service_hash) but also sets self.metadata directly, so
        # generate() does not then try to read it back off disk for a service this
        # test never actually wrote there.
        return celaut_pb2.Metadata(
            hashtag=celaut_pb2.Metadata.HashTag(
                hash=[celaut_pb2.Metadata.HashTag.Hash(type=b"t", value=b"v")]
            )
        )

    def test_a_client_sent_after_the_resolving_hash_still_succeeds(self):
        messages = [self._hash(), celaut_pb2.Client(client_id="known")]
        with patch.object(aisi, "require_caller", side_effect=_fake_require_caller):
            it = _RecordingIterable(_request(messages), _Context())
            list(it)
        self.assertEqual(it.generate_calls, 1)
        self.assertEqual(it.client_id, "known")

    def test_a_client_sent_before_the_resolving_hash_works_as_before(self):
        messages = [celaut_pb2.Client(client_id="known"), self._hash()]
        with patch.object(aisi, "require_caller", side_effect=_fake_require_caller):
            it = _RecordingIterable(_request(messages), _Context())
            list(it)
        self.assertEqual(it.generate_calls, 1)

    def test_no_client_ever_sent_refuses_at_the_end_not_mid_stream(self):
        messages = [self._hash()]
        with patch.object(aisi, "require_caller", side_effect=_fake_require_caller):
            it = _RecordingIterable(_request(messages), _Context())
            with self.assertRaises(ClientRequired):
                list(it)
        self.assertEqual(it.generate_calls, 0)

    def test_an_unrecognized_client_id_is_still_a_real_refusal_immediately(self):
        # Unlike "none yet", a client_id that arrived and was wrong is a final
        # answer -- must not be treated as "might still be coming".
        messages = [self._hash(), celaut_pb2.Client(client_id="not-known")]
        with patch.object(aisi, "require_caller", side_effect=_fake_require_caller):
            it = _RecordingIterable(_request(messages), _Context())
            with self.assertRaises(ClientRequired):
                list(it)
        self.assertEqual(it.generate_calls, 0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MissingServiceTests(unittest.TestCase):
    """Neither side has the service: the node refuses with an error, never with silence."""

    def _run(self, messages, found):
        iterable = _RecordingIterable(_request(messages), _Context())
        with patch.object(aisi, "find_service_hash", return_value=found), \
                patch.object(aisi, "add_wanted") as wanted, \
                patch.object(aisi, "require_caller", _fake_require_caller):
            with self.assertRaises(Exception) as caught:
                list(iterable)
        return str(caught.exception), wanted, iterable

    def test_a_hash_the_node_does_not_hold_is_refused_and_wanted(self):
        hash_message = celaut_pb2.Metadata.HashTag.Hash(type=b"t", value=b"v")
        reason, wanted, iterable = self._run(
            [hash_message, celaut_pb2.Client(client_id="known")], ("missing-hash", False)
        )
        self.assertIn("does not have the service missing-hash", reason)
        wanted.assert_called_once_with("missing-hash")
        self.assertEqual(iterable.generate_calls, 0)

    def test_a_request_with_no_usable_hash_is_refused(self):
        hash_message = celaut_pb2.Metadata.HashTag.Hash(type=b"other", value=b"v")
        reason, wanted, _ = self._run(
            [hash_message, celaut_pb2.Client(client_id="known")], (None, False)
        )
        self.assertIn("no hash of the type this node uses", reason)
        wanted.assert_not_called()



if __name__ == "__main__":
    unittest.main()

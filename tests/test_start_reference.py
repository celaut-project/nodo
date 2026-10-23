"""#578: a StartService request with a StartReference is idempotent.

The node answers a request with a reference that it knows from what the first request
with that reference did: the same instance, ABORTED while it runs, or the error of the
first start. Nothing is started and nothing is charged again.
"""
import sqlite3
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    import grpc
    from bee_rpc import client as bee
    from protos import celaut_pb2
    from protos.gateway_bee import StartService_input_indices
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    import src.gateway.iterables.abstract_input_service_iterable as aisi
    import src.gateway.iterables.start_service_iterable as ssi
    from src.utils import start_ref, utils
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

CLIENT = "client"
REFERENCE = b"\x01" * 32


class Aborted(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _Context:
    def __init__(self, active=True):
        self.active = active

    def peer(self) -> str:
        return "ipv4:9.9.9.9:4442"

    def is_active(self) -> bool:
        return self.active

    def abort(self, code, message):
        raise Aborted(code, message)


def _request(messages):
    return iter(list(bee.serialize_to_buffer(
        message_iterator=messages, indices=dict(StartService_input_indices))))


def _service_hash():
    return celaut_pb2.Metadata(hashtag=celaut_pb2.Metadata.HashTag(
        hash=[celaut_pb2.Metadata.HashTag.Hash(type=b"t", value=b"v")]))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StartReferenceTests(unittest.TestCase):

    def setUp(self):
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        self.addCleanup(self._restore)
        self.sc = SQLConnection()

        self.launches = []
        self.launch_error = None
        self.stopped = []
        for patcher in (
                patch.object(aisi, "find_service_hash", return_value=("service", True)),
                patch.object(aisi, "require_caller"),
                patch.object(ssi, "require_caller"),
                patch.object(ssi, "read_service_from_disk", return_value=celaut_pb2.Service()),
                patch.object(ssi, "get_service_hex_main_hash", return_value="service"),
                patch.object(ssi, "launch_service", side_effect=self._launch),
                patch.object(ssi, "stop_instance",
                             side_effect=lambda token: self.stopped.append(token))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original

    def _launch(self, **kwargs):
        self.launches.append(kwargs)
        if self.launch_error:
            raise self.launch_error
        return celaut_pb2.ServiceInstance(token=f"token-{len(self.launches)}")

    def call(self, messages, context=None):
        """The ServiceInstance that the node answers."""
        out = list(ssi.StartServiceIterable(_request(messages), context or _Context()))
        answers = list(bee.parse_from_buffer(
            iter(out), indices={1: celaut_pb2.ServiceInstance}, partitions_message_mode=True))
        return answers[-1] if answers else None

    def start(self, reference=REFERENCE, client=CLIENT, context=None):
        messages = [celaut_pb2.Client(client_id=client)]
        if reference:
            messages.append(celaut_pb2.StartReference(ref=reference))
        return self.call(messages + [_service_hash()], context)

    def ask_again(self, reference=REFERENCE, client=CLIENT):
        """A request with the reference and no service."""
        return self.call([celaut_pb2.Client(client_id=client),
                          celaut_pb2.StartReference(ref=reference)])

    def test_a_request_asked_again_gets_the_same_instance_and_starts_nothing(self):
        first = self.start()

        self.assertEqual(self.ask_again().token, first.token)
        self.assertEqual(self.start().token, first.token)
        self.assertEqual(len(self.launches), 1)

    def test_the_reference_goes_to_the_launch(self):
        self.start()

        self.assertEqual(self.launches[0]["start_ref"], REFERENCE.hex())

    def test_the_same_reference_from_another_caller_is_another_start(self):
        first = self.start()
        second = self.start(client="another-client")

        self.assertNotEqual(first.token, second.token)
        self.assertEqual(len(self.launches), 2)

    def test_a_request_with_no_reference_starts_each_time(self):
        self.start(reference=None)
        self.start(reference=None)

        self.assertEqual(len(self.launches), 2)
        self.assertNotIn("start_ref", [k for k, v in self.launches[0].items() if v])

    def test_a_start_that_runs_is_in_progress(self):
        self.sc.claim_start_ref(CLIENT, REFERENCE.hex())

        with self.assertRaises(Aborted) as caught:
            self.ask_again()

        self.assertEqual(caught.exception.code, grpc.StatusCode.ABORTED)
        self.assertEqual(self.launches, [])

    def test_a_failed_start_gives_its_error_again(self):
        self.launch_error = RuntimeError("no room")
        with self.assertRaises(RuntimeError):
            self.start()

        with self.assertRaises(Aborted) as caught:
            self.start()

        self.assertEqual(caught.exception.code, grpc.StatusCode.FAILED_PRECONDITION)
        self.assertIn("no room", str(caught.exception))
        self.assertEqual(len(self.launches), 1)

    def test_an_unknown_reference_with_no_service_is_refused(self):
        with patch.object(aisi, "find_service_hash", return_value=(None, False)):
            with self.assertRaises(Exception):
                self.ask_again()

        self.assertEqual(self.launches, [])

    def test_a_reference_of_more_than_64_bytes_is_refused(self):
        with self.assertRaises(Exception):
            self.start(reference=b"x" * (start_ref.MAX_BYTES + 1))

        self.assertEqual(self.launches, [])

    def test_an_instance_whose_answer_is_lost_is_kept_for_its_reference(self):
        first = self.start(context=_Context(active=False))

        self.assertEqual(self.stopped, [])
        self.assertEqual(self.ask_again().token, first.token)

    def test_an_instance_whose_answer_is_lost_with_no_reference_is_stopped(self):
        with self.assertRaises(Exception):
            self.start(reference=None, context=_Context(active=False))

        self.assertEqual(self.stopped, ["token-1"])

    def test_the_caller_sends_the_reference_before_the_hashes(self):
        messages = list(utils.service_extended(
            metadata=_service_hash(), send_only_hashes=True, client_id=CLIENT,
            start_ref=REFERENCE))

        kinds = [type(m) for m in messages]
        self.assertLess(kinds.index(celaut_pb2.StartReference),
                        kinds.index(celaut_pb2.Metadata.HashTag.Hash))

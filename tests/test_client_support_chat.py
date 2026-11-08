"""Support chat between a client and the operator: only the client_id is needed.

A client with no peer bound to it sends `Chat` and reads the answers with
`GetChatThread`. Pins what is stored (sequence, direction, the cap), that a client
reads only its own thread, that a client bound to a peer keeps the peer behaviour, and
the gateway wiring of the new RPC (same gate as `GetClientMetrics`).
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2
    from protos.gateway_bee import GATEWAY_RPCS
    from src.database import migrate
    from src.database.sql_connection import SQLConnection, TRACEABILITY_TABLES
    from src.gateway import client_gate
    from src.gateway import gateway as gateway_module
    import src.manager.chat as chat
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


class _Context:
    def peer(self):
        return "ipv4:9.9.9.9:4442"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ClientSupportChatTests(unittest.TestCase):

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        migrate.create_tables(self.conn.cursor())
        self.conn.commit()
        self._orig_conn = SQLConnection._connection
        SQLConnection._connection = self.conn
        self.sc = SQLConnection()

        self.client_id = uuid4().hex
        self.sc.add_client(client_id=self.client_id, balance_mu=0, last_usage=None)
        self.other_id = uuid4().hex
        self.sc.add_client(client_id=self.other_id, balance_mu=0, last_usage=None)

    def tearDown(self):
        SQLConnection._connection = self._orig_conn
        self.conn.close()
        os.unlink(self.db_path)

    def send(self, client_id, body="help", **fields):
        return chat.receive_chat_message(
            celaut_pb2.ChatMessage(client_id=client_id, body=body, **fields))

    def test_a_client_without_a_peer_is_stored_in_its_thread(self):
        self.assertIsNone(self.send(self.client_id, "my instance is down"))

        thread = chat.get_client_thread(self.client_id)
        self.assertEqual(thread.latest, 1)
        self.assertEqual(len(thread.entries), 1)
        entry = thread.entries[0]
        self.assertEqual((entry.sequence, entry.body, entry.from_operator),
                         (1, "my instance is down", False))
        self.assertGreater(entry.ts, 0)

    def test_the_sequence_starts_at_one_and_grows_by_one(self):
        self.send(self.client_id, "a")
        self.send(self.client_id, "b")
        chat.reply_to_client(self.client_id, "c")

        thread = chat.get_client_thread(self.client_id)
        self.assertEqual([e.sequence for e in thread.entries], [1, 2, 3])
        self.assertEqual([e.from_operator for e in thread.entries], [False, False, True])
        self.assertEqual(thread.latest, 3)

    def test_after_sequence_returns_only_what_comes_after(self):
        for body in ("a", "b", "c"):
            self.send(self.client_id, body)

        thread = chat.get_client_thread(self.client_id, after_sequence=1)
        self.assertEqual([e.body for e in thread.entries], ["b", "c"])
        caught_up = chat.get_client_thread(self.client_id, after_sequence=3)
        self.assertEqual((len(caught_up.entries), caught_up.latest), (0, 3))

    def test_an_empty_thread_has_latest_zero(self):
        thread = chat.get_client_thread(self.client_id)
        self.assertEqual((len(thread.entries), thread.latest), (0, 0))

    def test_a_client_reads_only_its_own_thread(self):
        self.send(self.client_id, "mine")
        self.send(self.other_id, "theirs")

        self.assertEqual([e.body for e in chat.get_client_thread(self.client_id).entries], ["mine"])
        self.assertEqual([e.body for e in chat.get_client_thread(self.other_id).entries], ["theirs"])
        self.assertEqual(chat.get_client_thread(self.other_id).entries[0].sequence, 1)

    def test_unknown_client_is_refused(self):
        with self.assertRaises(chat.ChatError):
            self.send(uuid4().hex)

    def test_empty_and_oversize_bodies_are_refused_and_not_stored(self):
        with self.assertRaises(chat.ChatError):
            self.send(self.client_id, "")
        with self.assertRaises(chat.ChatError):
            self.send(self.client_id, "x" * (chat.MAX_MESSAGE_BYTES + 1))
        self.assertEqual(chat.get_client_thread(self.client_id).latest, 0)

    def test_a_conversation_id_or_a_service_is_refused_for_a_plain_client(self):
        with self.assertRaises(chat.ChatError):
            self.send(self.client_id, "hi", conversation_id=uuid4().hex)
        with self.assertRaises(chat.ChatError):
            self.send(self.client_id, "hi", service=celaut_pb2.Metadata())
        self.assertEqual(chat.get_client_thread(self.client_id).latest, 0)

    def test_the_thread_is_cut_to_the_cap_and_sequences_do_not_change(self):
        with patch.object(chat, "MAX_STORED_MESSAGES_PER_PEER", 3):
            for n in range(5):
                self.send(self.client_id, f"m{n}")
            self.send(self.other_id, "other")

        thread = chat.get_client_thread(self.client_id)
        self.assertEqual([e.sequence for e in thread.entries], [3, 4, 5])
        self.assertEqual([e.body for e in thread.entries], ["m2", "m3", "m4"])
        self.assertEqual(thread.latest, 5)
        self.assertEqual(chat.get_client_thread(self.other_id).latest, 1)
        # The next message goes on from the last one: it never reuses a sequence.
        self.send(self.client_id, "m5")
        self.assertEqual(chat.get_client_thread(self.client_id).latest, 6)

    def test_an_answer_has_a_cap_per_reply(self):
        with patch.object(chat, "MAX_THREAD_ENTRIES_PER_ANSWER", 2):
            for n in range(5):
                self.send(self.client_id, f"m{n}")
            thread = chat.get_client_thread(self.client_id)
            self.assertEqual([e.sequence for e in thread.entries], [1, 2])
            self.assertEqual(thread.latest, 5)
            more = chat.get_client_thread(self.client_id, after_sequence=2)
            self.assertEqual([e.sequence for e in more.entries], [3, 4])

    def test_a_client_bound_to_a_peer_keeps_the_peer_behaviour(self):
        peer_id = "b" * 66
        self.sc.add_peer(peer_id=peer_id, advertisement=b"")
        self.sc.set_peer_local_client(peer_id=peer_id, client_id=self.client_id)

        self.assertEqual(self.send(self.client_id, "from the peer"), peer_id)

        self.assertEqual(len(self.sc.get_chat_messages(peer_id=peer_id)), 1)
        self.assertEqual(chat.get_client_thread(self.client_id).latest, 0)
        with self.assertRaises(chat.ChatError):
            chat.reply_to_client(self.client_id, "no thread for a peer")

    def test_the_operator_lists_reads_and_answers(self):
        self.send(self.client_id, "question")
        self.send(self.other_id, "another")
        chat.reply_to_client(self.other_id, "answer")

        waiting = {t["client_id"]: t for t in chat.list_client_threads()}
        self.assertEqual(set(waiting), {self.client_id, self.other_id})
        self.assertFalse(waiting[self.client_id]["last_from_operator"])
        self.assertTrue(waiting[self.other_id]["last_from_operator"])
        self.assertEqual(waiting[self.other_id]["messages"], 2)
        history = chat.get_client_thread_history(self.other_id)
        self.assertEqual([m["body"] for m in history], ["another", "answer"])

    def test_reply_to_an_unknown_client_or_empty_body_is_refused(self):
        with self.assertRaises(chat.ChatError):
            chat.reply_to_client(uuid4().hex, "hi")
        with self.assertRaises(chat.ChatError):
            chat.reply_to_client(self.client_id, "")

    def test_the_table_is_created_on_an_old_database(self):
        connection = sqlite3.connect(":memory:")
        try:
            cursor = connection.cursor()
            migrate.ensure_tables(cursor, TRACEABILITY_TABLES)
            tables = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("client_chat_messages", tables)
        finally:
            connection.close()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GetChatThreadGatewayTests(unittest.TestCase):

    def call(self, request_client_id, identified_as, after=0):
        parsed = (celaut_pb2.ChatThreadRequest(after_sequence=after), request_client_id)
        with patch.object(gateway_module, "parse_with_client", return_value=parsed), \
                patch.object(gateway_module, "require_caller", return_value=identified_as), \
                patch.object(gateway_module, "get_client_thread",
                             return_value=celaut_pb2.ChatThread(latest=7)) as read, \
                patch.object(gateway_module.BeeClient, "respond",
                             side_effect=lambda message_iterator, indices=None: iter([message_iterator])):
            try:
                return list(gateway_module.Gateway().GetChatThread(iter(()), _Context())), read
            except client_gate.ClientRequired as e:
                return e, read

    def test_a_client_reads_its_own_thread_from_the_cursor(self):
        result, read = self.call("c1", "c1", after=4)

        read.assert_called_once_with(client_id="c1", after_sequence=4)
        self.assertEqual(result[0].latest, 7)

    def test_a_local_instance_has_no_thread(self):
        error, read = self.call("c1", "some-instance")

        self.assertIsInstance(error, client_gate.ClientRequired)
        read.assert_not_called()

    def test_an_unknown_client_is_refused_by_the_gate(self):
        with patch.object(gateway_module, "parse_with_client",
                          return_value=(None, "nobody")), \
                patch.object(client_gate, "get_internal_service_id_by_uri", return_value=None), \
                patch.object(client_gate.sc, "client_exists", return_value=False):
            with self.assertRaises(client_gate.ClientRequired):
                list(gateway_module.Gateway().GetChatThread(iter(()), _Context()))

    def test_the_rpc_table_takes_a_request_and_a_client_and_answers_a_thread(self):
        rpc = GATEWAY_RPCS["GetChatThread"]
        self.assertEqual(rpc.input, {1: celaut_pb2.ChatThreadRequest, 2: celaut_pb2.Client})
        self.assertEqual(rpc.output, {1: celaut_pb2.ChatThread})
        self.assertEqual(rpc.auth, "client")


if __name__ == "__main__":
    unittest.main()

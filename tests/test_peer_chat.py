"""Chat (issue: peer chat): a signed, size-capped message channel between peers.

Pins the properties the RPC's own authentication depends on -- the transport gives
a server no verified caller identity of its own (see grpc_transport's module
docstring), so everything here rests on the signature: a message from a peer this
node has actually introduced itself with is accepted and stored; one that is
forged, replayed, oversize, or from a stranger is not. Also pins the schema
upgrade path (a node that only restarts must still get the new table and column,
see test_schema_upgrade_without_reinstall.py) and the client_id association a
Chat message may carry (peer.local_client_id).
"""
import os
import sqlite3
import tempfile
import unittest

IMPORT_ERROR = None
try:
    from mnemonic import Mnemonic

    from protos import celaut_pb2
    celaut_pb2.ChatMessage  # noqa: B018 -- absent until `bash/generate_protos.sh` regenerates it
    from src.database import migrate
    from src.database.sql_connection import SQLConnection, TRACEABILITY_COLUMNS, TRACEABILITY_TABLES
    from src.identity import node_identity as ni
    import src.manager.chat as chat
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerChatTests(unittest.TestCase):
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

        self.mnemonic = Mnemonic("english").generate(strength=128)
        self.peer_id, self._peer_key = ni._cached_keypair(self.mnemonic)
        self.sc.add_peer(peer_id=self.peer_id, advertisement=b"")

    def tearDown(self):
        SQLConnection._connection = self._orig_conn
        self.conn.close()
        os.unlink(self.db_path)

    def _signed(self, body: str, ts: int, client_id: str = "") -> "celaut_pb2.ChatMessage":
        payload = ni.chat_message_payload(self.peer_id, ts, body)
        signature = self._peer_key.sign(payload.encode("utf-8")).hex()
        kwargs = dict(peer_id=self.peer_id, ts=ts, body=body, signature=signature)
        if client_id:
            kwargs["client_id"] = client_id
        return celaut_pb2.ChatMessage(**kwargs)

    def test_a_signed_message_from_a_known_peer_is_stored(self):
        chat.receive_chat_message(self._signed("hi, is your instance ok?", ts=100))

        history = self.sc.get_chat_messages(peer_id=self.peer_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["body"], "hi, is your instance ok?")
        self.assertFalse(history[0]["from_us"])

    def test_a_replayed_message_is_rejected(self):
        message = self._signed("hello", ts=100)
        chat.receive_chat_message(message)
        with self.assertRaises(chat.ChatError):
            chat.receive_chat_message(message)
        self.assertEqual(len(self.sc.get_chat_messages(peer_id=self.peer_id)), 1)

    def test_a_forged_message_is_rejected(self):
        message = self._signed("original", ts=100)
        message.body = "not what was signed"
        with self.assertRaises(chat.ChatError):
            chat.receive_chat_message(message)
        self.assertEqual(self.sc.get_chat_messages(peer_id=self.peer_id), [])

    def test_a_stranger_is_rejected_even_with_a_valid_signature(self):
        stranger_mnemonic = Mnemonic("english").generate(strength=128)
        stranger_id, stranger_key = ni._cached_keypair(stranger_mnemonic)
        ts = 100
        payload = ni.chat_message_payload(stranger_id, ts, "hi")
        message = celaut_pb2.ChatMessage(
            peer_id=stranger_id, ts=ts, body="hi",
            signature=stranger_key.sign(payload.encode("utf-8")).hex(),
        )
        self.assertFalse(self.sc.peer_exists(peer_id=stranger_id))
        with self.assertRaises(chat.ChatError):
            chat.receive_chat_message(message)

    def test_an_oversize_message_is_rejected(self):
        oversize = "x" * (chat.MAX_MESSAGE_BYTES + 1)
        with self.assertRaises(chat.ChatError):
            chat.receive_chat_message(self._signed(oversize, ts=100))
        self.assertEqual(self.sc.get_chat_messages(peer_id=self.peer_id), [])

    def test_a_message_carrying_a_known_client_id_associates_it_with_the_peer(self):
        client_id = "11111111111111111111111111111111"
        self.sc.add_client(client_id=client_id, balance_mu=0, last_usage=None)

        chat.receive_chat_message(self._signed("this is my client_id", ts=100, client_id=client_id))

        self.assertEqual(self.sc.get_peer_local_client_id(peer_id=self.peer_id), client_id)
        self.assertEqual(self.sc.get_peer_id_by_local_client(client_id=client_id), self.peer_id)

    def test_an_unknown_client_id_is_not_associated(self):
        chat.receive_chat_message(
            self._signed("bogus client_id", ts=100, client_id="deadbeef" * 4)
        )
        self.assertIsNone(self.sc.get_peer_local_client_id(peer_id=self.peer_id))

    def test_add_chat_message_prunes_each_peer_to_its_own_ceiling(self):
        for i in range(5):
            self.sc.add_chat_message(
                peer_id=self.peer_id, from_us=(i % 2 == 0), body=f"msg {i}",
                ts=1000 + i, keep_per_peer=3,
            )
        history = self.sc.get_chat_messages(peer_id=self.peer_id, limit=100)
        self.assertEqual([h["body"] for h in history], ["msg 2", "msg 3", "msg 4"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChatSchemaUpgradeTests(unittest.TestCase):
    """A node that only restarts, never runs `nodo migrate`, must still get this."""

    def test_peer_chat_messages_is_a_traceability_table(self):
        self.assertIn("peer_chat_messages", TRACEABILITY_TABLES)

    def test_local_client_id_is_an_additive_peer_column(self):
        self.assertIn("peer", TRACEABILITY_COLUMNS)
        self.assertIn("local_client_id", TRACEABILITY_COLUMNS["peer"])

    def test_a_pre_existing_peer_table_gets_the_column(self):
        connection = sqlite3.connect(":memory:")
        try:
            cursor = connection.cursor()
            cursor.execute(
                "CREATE TABLE peer (id TEXT PRIMARY KEY, remote_client_id TEXT)"
            )
            migrate.ensure_columns(cursor, "peer", TRACEABILITY_COLUMNS["peer"])
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(peer)")}
            self.assertIn("local_client_id", columns)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

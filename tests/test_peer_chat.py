"""Chat (issue: peer chat): a client-authenticated, size-capped message channel.

Chat itself authenticates exactly like every other client-facing RPC on this
gateway: `client_id` is a bearer credential (like `TokenMessage.token`), not a
fresh signature scheme. The interesting property to pin is therefore *where* a
client_id gets tied to a peer -- at `GenerateClient` time
(`manager._created_client`), the one moment a freshly minted id and a verifiable
peer identity exist together -- and that Chat refuses a client_id it never tied
to anyone, rather than guessing. Also pins the schema upgrade path (a node that
only restarts must still get the new table and column, see
test_schema_upgrade_without_reinstall.py).
"""
import os
import sqlite3
import tempfile
import unittest
from uuid import uuid4

IMPORT_ERROR = None
try:
    from mnemonic import Mnemonic

    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2
    from src.database import migrate
    from src.database.sql_connection import SQLConnection, TRACEABILITY_COLUMNS, TRACEABILITY_TABLES
    from src.identity import node_identity as ni
    import src.manager.chat as chat
    import src.manager.manager as manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

# `ChatMessage`, and `Client`'s new `peer_id`/`signature` fields, only exist once
# `bash/generate_protos.sh` has regenerated `protos/celaut_pb2.py` for this RPC.
# `generate_client_or_pow_required` itself takes plain strings, not a protobuf
# `Client`, so `ClientPeerBindingTests` below needs none of this and can run for
# real before that regeneration; only `PeerChatTests` (which constructs a
# `ChatMessage`) is gated on it.
CHAT_PROTO_ERROR = IMPORT_ERROR
if CHAT_PROTO_ERROR is None:
    try:
        celaut_pb2.ChatMessage
    except AttributeError as chat_proto_exc:
        CHAT_PROTO_ERROR = chat_proto_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ClientPeerBindingTests(unittest.TestCase):
    """Where `peer.local_client_id` actually gets set: GenerateClient, not Chat."""

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

    def _binding(self, client_id: str, peer_id: str = None, key=None) -> dict:
        peer_id = peer_id if peer_id is not None else self.peer_id
        key = key or self._peer_key
        payload = ni.client_binding_payload(peer_id, client_id)
        return {"peer_id": peer_id, "signature": key.sign(payload.encode("utf-8")).hex()}

    def test_a_verified_binding_associates_the_new_client_with_the_peer(self):
        client_id = uuid4().hex
        result = manager.generate_client_or_pow_required(
            client_id=client_id, **self._binding(client_id)
        )
        self.assertEqual(result.client_id, client_id)
        self.assertTrue(self.sc.client_exists(client_id=client_id))
        self.assertEqual(self.sc.get_peer_local_client_id(peer_id=self.peer_id), client_id)

    def test_no_binding_data_creates_an_unassociated_client(self):
        client_id = uuid4().hex
        result = manager.generate_client_or_pow_required(client_id=client_id)
        self.assertEqual(result.client_id, client_id)
        self.assertTrue(self.sc.client_exists(client_id=client_id))
        self.assertIsNone(self.sc.get_peer_local_client_id(peer_id=self.peer_id))

    def test_a_bad_signature_still_creates_the_client_but_does_not_associate_it(self):
        client_id = uuid4().hex
        result = manager.generate_client_or_pow_required(
            client_id=client_id, peer_id=self.peer_id, signature="not-a-real-signature",
        )
        self.assertEqual(result.client_id, client_id)
        self.assertIsNone(self.sc.get_peer_local_client_id(peer_id=self.peer_id))

    def test_a_signature_for_a_different_client_id_does_not_associate(self):
        client_id = uuid4().hex
        other_id = uuid4().hex
        binding = self._binding(other_id)  # signed over the WRONG client_id
        result = manager.generate_client_or_pow_required(client_id=client_id, **binding)
        self.assertEqual(result.client_id, client_id)
        self.assertIsNone(self.sc.get_peer_local_client_id(peer_id=self.peer_id))

    def test_binding_to_an_unknown_peer_does_not_associate(self):
        stranger_mnemonic = Mnemonic("english").generate(strength=128)
        stranger_id, stranger_key = ni._cached_keypair(stranger_mnemonic)
        client_id = uuid4().hex
        result = manager.generate_client_or_pow_required(
            client_id=client_id, **self._binding(client_id, peer_id=stranger_id, key=stranger_key)
        )
        self.assertEqual(result.client_id, client_id)
        self.assertIsNone(self.sc.get_peer_local_client_id(peer_id=stranger_id))


@unittest.skipIf(CHAT_PROTO_ERROR is not None, f"Missing runtime dependencies: {CHAT_PROTO_ERROR}")
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

        self.peer_id = "a" * 66
        self.sc.add_peer(peer_id=self.peer_id, advertisement=b"")
        self.client_id = uuid4().hex
        self.sc.add_client(client_id=self.client_id, balance_mu=0, last_usage=None)
        self.sc.set_peer_local_client(peer_id=self.peer_id, client_id=self.client_id)

    def tearDown(self):
        SQLConnection._connection = self._orig_conn
        self.conn.close()
        os.unlink(self.db_path)

    def test_a_message_from_an_associated_client_is_stored_under_its_peer(self):
        message = celaut_pb2.ChatMessage(client_id=self.client_id, body="is your instance ok?")
        got_peer_id = chat.receive_chat_message(message)

        self.assertEqual(got_peer_id, self.peer_id)
        history = self.sc.get_chat_messages(peer_id=self.peer_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["body"], "is your instance ok?")
        self.assertFalse(history[0]["from_us"])

    def test_an_unknown_client_id_is_rejected(self):
        message = celaut_pb2.ChatMessage(client_id=uuid4().hex, body="hi")
        with self.assertRaises(chat.ChatError):
            chat.receive_chat_message(message)

    def test_a_client_id_never_associated_with_a_peer_is_rejected(self):
        lonely_client_id = uuid4().hex
        self.sc.add_client(client_id=lonely_client_id, balance_mu=0, last_usage=None)
        message = celaut_pb2.ChatMessage(client_id=lonely_client_id, body="hi")
        with self.assertRaises(chat.ChatError):
            chat.receive_chat_message(message)

    def test_an_empty_message_is_rejected(self):
        message = celaut_pb2.ChatMessage(client_id=self.client_id, body="")
        with self.assertRaises(chat.ChatError):
            chat.receive_chat_message(message)
        self.assertEqual(self.sc.get_chat_messages(peer_id=self.peer_id), [])

    def test_an_oversize_message_is_rejected(self):
        message = celaut_pb2.ChatMessage(
            client_id=self.client_id, body="x" * (chat.MAX_MESSAGE_BYTES + 1)
        )
        with self.assertRaises(chat.ChatError):
            chat.receive_chat_message(message)
        self.assertEqual(self.sc.get_chat_messages(peer_id=self.peer_id), [])

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

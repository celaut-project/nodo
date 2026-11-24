"""Chat: how a client_id is tied to a peer, and the shared-service cards (issue #438).

Chat authenticates like every other client-facing RPC on this gateway: `client_id` is a
bearer credential (like `TokenMessage.token`), not a fresh signature scheme. The property
to pin is *where* a client_id gets tied to a peer -- at `GenerateClient` time
(`manager._created_client`), the one moment a freshly minted id and a verifiable peer
identity exist together. The conversations themselves are in `test_chat_conversations.py`.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

IMPORT_ERROR = None
try:
    from mnemonic import Mnemonic

    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
    from src.identity import node_identity as ni
    import src.manager.chat as chat
    import src.manager.manager as manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


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


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServiceCardTests(unittest.TestCase):
    """A service shared in chat as a card (issue #438, `ChatMessage.service`)."""

    SERVICE_ID = "ab" * 32

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

    def _metadata(self, service_id=None, tags=("hello-world",), hash_type=None):
        from src.utils.hashing import SHA3_256_ID, SHAKE_256_ID
        metadata = celaut_pb2.Metadata()
        metadata.hashtag.hash.add(
            type=hash_type or SHA3_256_ID, value=bytes.fromhex(service_id or self.SERVICE_ID),
        )
        metadata.hashtag.hash.add(type=SHAKE_256_ID, value=bytes.fromhex("cd" * 32))
        metadata.hashtag.tag.extend(tags)
        return metadata

    def _incoming(self, text, metadata):
        line = chat.service_fallback_line(metadata)
        message = celaut_pb2.ChatMessage(
            client_id=self.client_id, body=f"{text}\n{line}" if text else line,
        )
        message.service.CopyFrom(metadata)
        return message

    def _stored_metadata(self):
        blob = self.conn.execute("SELECT service_metadata FROM chat_messages").fetchone()[0]
        return celaut_pb2.Metadata.FromString(blob)

    def _history(self):
        return self.sc.get_conversation_messages(client_id=self.client_id, conversation=1)

    def test_a_shared_service_is_stored_as_its_metadata_and_not_twice_as_text(self):
        metadata = self._metadata()
        self.assertEqual(chat.receive_chat_message(self._incoming("have a look", metadata)), (1, 1))

        history = self._history()
        self.assertEqual(history[0]["body"], "have a look")
        self.assertEqual(
            history[0]["service"], {"id": self.SERVICE_ID, "tags": ["hello-world"]}
        )
        self.assertEqual(self._stored_metadata(), metadata, "the Metadata is kept whole")

    def test_a_card_on_its_own_is_a_message(self):
        chat.receive_chat_message(self._incoming("", self._metadata(tags=())))

        history = self._history()
        self.assertEqual(history[0]["body"], "")
        self.assertEqual(history[0]["service"], {"id": self.SERVICE_ID, "tags": []})

    def test_the_id_is_the_hash_of_this_nodes_registry_type_or_none(self):
        """No hash of the configured type: stored, but with no id to get it by."""
        from src.utils.hashing import BLAKE2B_ID
        metadata = self._metadata(hash_type=BLAKE2B_ID)
        metadata.hashtag.hash.pop(-1)  # leave only the BLAKE2B one
        message = celaut_pb2.ChatMessage(client_id=self.client_id, body="look")
        message.service.CopyFrom(metadata)

        chat.receive_chat_message(message)

        history = self._history()
        self.assertEqual(history[0]["service"], {"id": None, "tags": ["hello-world"]})

    def test_the_fallback_is_stripped_whichever_hash_the_sender_named_it_by(self):
        from src.utils.hashing import SHAKE_256_ID
        metadata = self._metadata()
        line = f"[service {'cd' * 32} (hello-world) -- get it with: nodo get {'cd' * 32}]"
        self.assertEqual(metadata.hashtag.hash[1].type, SHAKE_256_ID)
        message = celaut_pb2.ChatMessage(client_id=self.client_id, body=f"look\n{line}")
        message.service.CopyFrom(metadata)

        chat.receive_chat_message(message)

        self.assertEqual(self._history()[0]["body"], "look")

    def test_an_ordinary_message_has_no_card(self):
        chat.receive_chat_message(celaut_pb2.ChatMessage(client_id=self.client_id, body="hi"))

        self.assertIsNone(self._history()[0]["service"])

    def test_the_fallback_names_the_service_for_a_node_without_cards(self):
        line = chat.service_fallback_line(self._metadata())
        self.assertIn(self.SERVICE_ID, line)
        self.assertIn("hello-world", line)
        self.assertIn(f"nodo get {self.SERVICE_ID}", line)

    def test_malformed_metadata_refuses_the_whole_message(self):
        no_hashes = self._metadata()
        del no_hashes.hashtag.hash[:]
        empty_value = self._metadata()
        empty_value.hashtag.hash[0].value = b""
        too_many_hashes = self._metadata()
        for _ in range(chat.MAX_SERVICE_HASHES):
            too_many_hashes.hashtag.hash.add(type=b"t", value=b"v")
        long_hash = self._metadata()
        long_hash.hashtag.hash[0].value = b"x" * (chat.MAX_SERVICE_HASH_BYTES + 1)
        oversize = self._metadata()
        oversize.format.prose = "p" * chat.MAX_SERVICE_METADATA_BYTES
        for metadata in (
            no_hashes,
            empty_value,
            too_many_hashes,
            long_hash,
            oversize,
            self._metadata(tags=["x"] * (chat.MAX_SERVICE_TAGS + 1)),
            self._metadata(tags=["two\nlines"]),
            self._metadata(tags=["y" * (chat.MAX_SERVICE_TAG_CHARS + 1)]),
        ):
            message = celaut_pb2.ChatMessage(client_id=self.client_id, body="look")
            message.service.CopyFrom(metadata)
            with self.assertRaises(chat.ChatError):
                chat.receive_chat_message(message)
        self.assertEqual(self._history(), [])

    def test_sending_puts_the_metadata_on_the_wire_and_the_fallback_in_the_body(self):
        sent = []
        metadata = self._metadata()
        remote_client = uuid4().hex
        with patch.object(chat, "local_service_metadata", return_value=metadata), \
             patch.object(chat.sc, "get_peer_client", return_value=remote_client), \
             patch.object(chat, "peer_channel", return_value=None), \
             patch.object(chat.BeeClient, "get_chat_thread",
                          return_value=celaut_pb2.ChatThread()), \
             patch.object(chat.BeeClient, "chat",
                          side_effect=lambda _channel, message: sent.append(message)
                          or celaut_pb2.ChatAck(stored=True, conversation_id=1, sequence=1)):
            ref = chat.open_conversation(
                peer_id=self.peer_id, topic="try", body="look", service="hello-world")

        self.assertEqual(ref, f"{remote_client}#1")
        self.assertEqual(sent[0].service, metadata)
        self.assertEqual(sent[0].conversation_id, 0, "0 opens a conversation")
        self.assertEqual(sent[0].topic, "try")
        self.assertTrue(sent[0].body.startswith("look\n"), sent[0].body)
        self.assertIn(self.SERVICE_ID, sent[0].body)

    def test_only_a_service_in_the_local_registry_can_be_shared(self):
        import src.utils.utils as utils
        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "registry")
            metadata_dir = os.path.join(tmp, "metadata")
            os.makedirs(os.path.join(registry, self.SERVICE_ID))
            os.makedirs(os.path.join(registry, "ef" * 32))
            os.makedirs(metadata_dir)
            meta = self._metadata(tags=("hello-world", "demo"))
            meta.format.tags.append("linux/amd64")
            with open(os.path.join(metadata_dir, self.SERVICE_ID), "wb") as f:
                f.write(meta.SerializeToString())
            # Held, but its Metadata names it by another id: not shareable.
            with open(os.path.join(metadata_dir, "ef" * 32), "wb") as f:
                f.write(meta.SerializeToString())
            paths = {"REGISTRY": registry, "METADATA_REGISTRY": metadata_dir}
            with patch.object(chat.env_manager, "get", side_effect=lambda key, default=None: paths.get(key, default)), \
                 patch.object(utils, "METADATA_REGISTRY", metadata_dir), \
                 patch("src.commands.__by_tag.METADATA", metadata_dir):
                self.assertEqual(chat.local_service_metadata(self.SERVICE_ID), meta)
                with self.assertRaises(chat.ChatError):
                    chat.local_service_metadata("cd" * 32)
                with self.assertRaises(chat.ChatError):
                    chat.local_service_metadata("ef" * 32)


if __name__ == "__main__":
    unittest.main()

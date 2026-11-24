"""The signature of a chat entry, and the conversation as evidence.

A node signs each message that it stores, and chains the messages of a conversation. The
tests pin: the text that is signed (in the order of the numbers of the fields, with a hash
of the client_id and never the client_id), what a change of each field does, the chain, the
node that signs what it stores, and the exported document that others check with no node.
"""
import hashlib
import json
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
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
    from src.identity import chat_signature, node_identity
    import src.manager.chat as chat
    from tests.reputation_system.test_offchain_reputation import _Signer
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _blake(text):
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def _hex(text):
    return text.encode("utf-8").hex()


def _sign(signer, client_id, entry):
    digest = chat_signature.entry_hash(chat_signature.client_ref(client_id), entry)
    with signer:
        entry.signature = node_identity.sign_peer_payload(
            node_identity.chat_entry_payload(signer.peer_id, digest))
    return entry


def _service():
    metadata = celaut_pb2.Metadata()
    metadata.hashtag.hash.add(type=b"\x01\x02", value=b"\xaa\xbb")
    metadata.hashtag.tag.extend(["hello", "a/b"])
    return metadata


def _entry(client_id, previous=None, **fields):
    ref = chat_signature.client_ref(client_id)
    entry = celaut_pb2.ChatEntry(
        sequence=7, conversation_id=2, ts=1798329600, from_operator=True, body="the text",
        prev_hash=chat_signature.genesis_hash(ref, 2), **fields)
    return entry


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EntryDigestTests(unittest.TestCase):
    client_id = "the-client-id"

    def test_the_client_is_a_hash_and_never_the_client_id(self):
        ref = chat_signature.client_ref(self.client_id)
        self.assertEqual(ref, _blake("celaut-chat-client:" + self.client_id))
        self.assertNotIn(self.client_id, ref)

    def test_the_start_of_a_conversation(self):
        ref = chat_signature.client_ref(self.client_id)
        self.assertEqual(chat_signature.genesis_hash(ref, 3), _blake(f"celaut-chat-genesis:{ref}#3"))
        self.assertNotEqual(chat_signature.genesis_hash(ref, 3), chat_signature.genesis_hash(ref, 4))

    def test_the_text_is_in_the_order_of_the_numbers_of_the_fields(self):
        entry = _entry(self.client_id)
        entry.service.CopyFrom(_service())
        ref = chat_signature.client_ref(self.client_id)
        expected = "celaut-chat-entry:" + "|".join((
            ref,                                      # the client: not a field
            "7",                                      # sequence = 1
            "2",                                      # conversation_id = 2
            "1798329600",                             # ts = 3
            "1",                                      # from_operator = 4
            _hex("the text"),                         # body = 5
            f"0102~aabb~{_hex('hello')}/{_hex('a/b')}",  # service = 6
            entry.prev_hash,                          # prev_hash = 7
        ))
        self.assertEqual(chat_signature.entry_hash(ref, entry), _blake(expected))

    def test_an_empty_part_is_empty_and_keeps_its_place(self):
        entry = _entry(self.client_id)
        entry.from_operator = False
        entry.body = ""
        ref = chat_signature.client_ref(self.client_id)
        expected = "celaut-chat-entry:" + "|".join((
            ref, "7", "2", "1798329600", "0", "", "", entry.prev_hash))
        self.assertEqual(chat_signature.entry_hash(ref, entry), _blake(expected))

    def test_every_field_but_the_signature_moves_the_digest(self):
        ref = chat_signature.client_ref(self.client_id)
        base = chat_signature.entry_hash(ref, _entry(self.client_id))
        changes = {
            "sequence": lambda e: setattr(e, "sequence", 8),
            "conversation_id": lambda e: setattr(e, "conversation_id", 3),
            "ts": lambda e: setattr(e, "ts", e.ts + 1),
            "from_operator": lambda e: setattr(e, "from_operator", not e.from_operator),
            "body": lambda e: setattr(e, "body", "another text"),
            "service": lambda e: e.service.CopyFrom(_service()),
            "prev_hash": lambda e: setattr(e, "prev_hash", "ee" * 32),
        }
        fields = {f.name for f in celaut_pb2.ChatEntry.DESCRIPTOR.fields}
        self.assertEqual(fields - {"signature"}, set(changes))
        for name, change in changes.items():
            with self.subTest(field=name):
                entry = _entry(self.client_id)
                change(entry)
                self.assertNotEqual(chat_signature.entry_hash(ref, entry), base)
        entry = _entry(self.client_id)
        entry.signature = "another"
        self.assertEqual(chat_signature.entry_hash(ref, entry), base)
        self.assertNotEqual(chat_signature.entry_hash(chat_signature.client_ref("other"),
                                                      _entry(self.client_id)), base)

    def test_the_tags_and_the_hashes_of_a_service_are_signed_and_the_rest_is_not(self):
        ref = chat_signature.client_ref(self.client_id)
        plain, with_prose = _entry(self.client_id), _entry(self.client_id)
        plain.service.CopyFrom(_service())
        with_prose.service.CopyFrom(_service())
        with_prose.service.format.prose = "not signed"
        self.assertEqual(chat_signature.entry_hash(ref, plain),
                         chat_signature.entry_hash(ref, with_prose))
        other_tag = _entry(self.client_id)
        other_tag.service.CopyFrom(_service())
        other_tag.service.hashtag.tag[0] = "changed"
        self.assertNotEqual(chat_signature.entry_hash(ref, plain),
                            chat_signature.entry_hash(ref, other_tag))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VerifyTests(unittest.TestCase):
    client_id = "the-client-id"

    def setUp(self):
        self.node = _Signer()
        self.ref = chat_signature.client_ref(self.client_id)

    def chain(self, count=3):
        entries, previous_hash = [], chat_signature.genesis_hash(self.ref, 2)
        for n in range(1, count + 1):
            entry = celaut_pb2.ChatEntry(
                sequence=n, conversation_id=2, ts=100 + n, from_operator=bool(n % 2),
                body=f"m{n}", prev_hash=previous_hash)
            _sign(self.node, self.client_id, entry)
            previous_hash = chat_signature.entry_hash(self.ref, entry)
            entries.append(entry)
        return entries

    def test_the_node_that_signed_verifies_and_nobody_else(self):
        [entry] = self.chain(1)
        self.assertTrue(chat_signature.verify_entry(entry, self.ref, self.node.peer_id))
        self.assertFalse(chat_signature.verify_entry(entry, self.ref, _Signer().peer_id))

    def test_a_change_of_any_field_breaks_the_signature(self):
        [entry] = self.chain(1)
        for name, change in {
            "body": lambda e: setattr(e, "body", "edited"),
            "ts": lambda e: setattr(e, "ts", 1),
            "sequence": lambda e: setattr(e, "sequence", 9),
            "from_operator": lambda e: setattr(e, "from_operator", not e.from_operator),
            "conversation_id": lambda e: setattr(e, "conversation_id", 5),
            "prev_hash": lambda e: setattr(e, "prev_hash", "00" * 32),
            "service": lambda e: e.service.CopyFrom(_service()),
        }.items():
            with self.subTest(field=name):
                copy = celaut_pb2.ChatEntry.FromString(entry.SerializeToString())
                change(copy)
                self.assertFalse(chat_signature.verify_entry(copy, self.ref, self.node.peer_id))
        self.assertFalse(chat_signature.verify_entry(
            entry, chat_signature.client_ref("another client"), self.node.peer_id),
            "the signature is for one client")

    def test_an_entry_with_no_signature_is_not_valid(self):
        [entry] = self.chain(1)
        entry.signature = ""
        self.assertFalse(chat_signature.verify_entry(entry, self.ref, self.node.peer_id))

    def test_a_chain_from_the_start_is_valid(self):
        self.assertEqual(chat_signature.verify_chain(self.chain(), self.ref, self.node.peer_id),
                         (True, True, ""))

    def test_a_chain_that_starts_later_is_valid_but_not_from_the_start(self):
        valid, from_start, _ = chat_signature.verify_chain(
            self.chain()[1:], self.ref, self.node.peer_id)
        self.assertEqual((valid, from_start), (True, False))

    def test_a_removed_message_in_the_middle_breaks_the_chain(self):
        entries = self.chain(4)
        del entries[1]
        valid, _, reason = chat_signature.verify_chain(entries, self.ref, self.node.peer_id)
        self.assertFalse(valid)
        self.assertIn("does not follow", reason)

    def test_an_added_message_breaks_the_chain(self):
        entries = self.chain(3)
        extra = celaut_pb2.ChatEntry(sequence=2, conversation_id=2, ts=1, body="added",
                                     prev_hash=chat_signature.entry_hash(self.ref, entries[0]))
        _sign(self.node, self.client_id, extra)
        valid, _, _ = chat_signature.verify_chain(
            [entries[0], extra, entries[1], entries[2]], self.ref, self.node.peer_id)
        self.assertFalse(valid)

    def test_the_order_and_the_conversation_are_checked(self):
        entries = self.chain(3)
        self.assertFalse(chat_signature.verify_chain(
            [entries[1], entries[0]], self.ref, self.node.peer_id)[0])
        other = celaut_pb2.ChatEntry(
            sequence=9, conversation_id=3, ts=1, body="x",
            prev_hash=chat_signature.entry_hash(self.ref, entries[2]))
        _sign(self.node, self.client_id, other)
        valid, _, reason = chat_signature.verify_chain(
            entries + [other], self.ref, self.node.peer_id)
        self.assertFalse(valid)
        self.assertIn("another conversation", reason)

    def test_one_bad_signature_in_a_chain_is_not_valid(self):
        entries = self.chain(3)
        entries[1].body = "edited"
        self.assertFalse(chat_signature.verify_chain(entries, self.ref, self.node.peer_id)[0])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServingNodeSignsTests(unittest.TestCase):
    """What the node that keeps the conversations signs and chains."""

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
        self.node = _Signer()
        self.node.__enter__()
        self.addCleanup(self.node.__exit__)
        self.client_id = uuid4().hex
        self.sc.add_client(client_id=self.client_id, balance_mu=0, last_usage=None)
        self.ref = chat_signature.client_ref(self.client_id)

    def tearDown(self):
        SQLConnection._connection = self._orig_conn
        self.conn.close()
        os.unlink(self.db_path)

    def send(self, body, conversation_id=0, **fields):
        return chat.receive_chat_message(celaut_pb2.ChatMessage(
            client_id=self.client_id, conversation_id=conversation_id, body=body, **fields))

    def test_the_receipt_is_signed_by_the_node_and_starts_the_chain(self):
        entry = self.send("my instance is down")
        self.assertEqual((entry.conversation_id, entry.sequence, entry.from_operator,
                          entry.body), (1, 1, False, "my instance is down"))
        self.assertGreater(entry.ts, 0)
        self.assertEqual(entry.prev_hash, chat_signature.genesis_hash(self.ref, 1))
        self.assertTrue(chat_signature.verify_entry(entry, self.ref, self.node.peer_id))

    def test_the_answers_of_the_operator_are_signed_and_chained(self):
        first = self.send("question")
        chat.reply(f"{self.client_id}#1", "answer")
        thread = chat.get_client_thread(self.client_id)
        self.assertEqual(
            chat_signature.verify_chain(thread.entries, self.ref, self.node.peer_id),
            (True, True, ""))
        self.assertTrue(thread.entries[1].from_operator)
        self.assertEqual(thread.entries[1].prev_hash, chat_signature.entry_hash(self.ref, first))

    def test_each_conversation_has_its_own_chain_and_the_sequence_is_shared(self):
        a1 = self.send("a1")
        b1 = self.send("b1")
        a2 = self.send("a2", conversation_id=1)
        self.assertEqual(b1.prev_hash, chat_signature.genesis_hash(self.ref, 2))
        self.assertEqual(a2.prev_hash, chat_signature.entry_hash(self.ref, a1))
        self.assertEqual([a1.sequence, b1.sequence, a2.sequence], [1, 2, 3])
        thread = chat.get_client_thread(self.client_id)
        for number in (1, 2):
            entries = [e for e in thread.entries if e.conversation_id == number]
            self.assertTrue(chat_signature.verify_chain(
                entries, self.ref, self.node.peer_id)[0])

    def test_a_shared_service_is_signed(self):
        from src.utils.hashing import SHA3_256_ID
        metadata = celaut_pb2.Metadata()
        metadata.hashtag.hash.add(type=SHA3_256_ID, value=bytes.fromhex("ab" * 32))
        metadata.hashtag.tag.extend(["hello", "a/b"])
        message = celaut_pb2.ChatMessage(client_id=self.client_id, body="look")
        message.body = f"look\n{chat.service_fallback_line(metadata)}"
        message.service.CopyFrom(metadata)
        entry = chat.receive_chat_message(message)
        self.assertEqual(entry.body, "look")
        self.assertEqual(list(entry.service.hashtag.tag), ["hello", "a/b"])
        self.assertTrue(chat_signature.verify_entry(entry, self.ref, self.node.peer_id))
        [row] = chat.get_client_thread(self.client_id).entries
        self.assertTrue(chat_signature.verify_entry(row, self.ref, self.node.peer_id))

    def test_the_chain_still_holds_when_the_cap_removed_the_oldest_messages(self):
        with patch.object(chat, "MAX_STORED_MESSAGES_PER_CLIENT", 3):
            for n in range(5):
                self.send(f"m{n}", conversation_id=1 if n else 0)
        thread = chat.get_client_thread(self.client_id)
        valid, from_start, _ = chat_signature.verify_chain(
            thread.entries, self.ref, self.node.peer_id)
        self.assertEqual((valid, from_start, len(thread.entries)), (True, False, 3))

    def test_a_node_with_no_identity_key_stores_the_message_with_no_signature(self):
        with patch.object(node_identity, "get_identity_mnemonic", return_value=None):
            entry = self.send("no key")
        self.assertEqual(entry.signature, "")
        self.assertFalse(chat_signature.verify_entry(entry, self.ref, self.node.peer_id))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EvidenceDocumentTests(unittest.TestCase):
    """The exported conversation, checked with no node and no database."""

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
        self.node = _Signer()
        self.node.__enter__()
        self.addCleanup(self.node.__exit__)
        self.client_id = uuid4().hex
        self.sc.add_client(client_id=self.client_id, balance_mu=0, last_usage=None)
        chat.receive_chat_message(celaut_pb2.ChatMessage(
            client_id=self.client_id, body="you charged me twice", topic="charge"))
        chat.reply(f"{self.client_id}#1", "we will look at it")
        self.ref = f"{self.client_id}#1"

    def tearDown(self):
        SQLConnection._connection = self._orig_conn
        self.conn.close()
        os.unlink(self.db_path)

    def document(self):
        return json.loads(json.dumps(chat.export_conversation(self.ref)))

    def test_an_exported_conversation_verifies(self):
        document = self.document()
        self.assertEqual(document["node"], self.node.peer_id)
        self.assertEqual(document["topic"], "charge")
        self.assertEqual([e["body"] for e in document["entries"]],
                         ["you charged me twice", "we will look at it"])
        self.assertEqual(chat.verify_document(document), {
            "valid": True, "from_start": True, "reason": "",
            "node": self.node.peer_id, "entries": 2})

    def test_the_document_never_holds_the_client_id(self):
        text = json.dumps(self.document())
        self.assertNotIn(self.client_id, text)
        self.assertIn(chat_signature.client_ref(self.client_id), text)

    def test_any_edit_of_the_document_makes_it_not_valid(self):
        edits = {
            "body": lambda d: d["entries"][0].__setitem__("body", "you charged me once"),
            "ts": lambda d: d["entries"][1].__setitem__("ts", 1),
            "direction": lambda d: d["entries"][0].__setitem__("from_operator", True),
            "signature": lambda d: d["entries"][1].__setitem__("signature", "00" * 64),
            "client": lambda d: d.__setitem__("client_ref", "ab" * 32),
            "node": lambda d: d.__setitem__("node", _Signer().peer_id),
            "removed": lambda d: d["entries"].pop(0) and None,
            "swapped": lambda d: d["entries"].reverse(),
        }
        for name, edit in edits.items():
            with self.subTest(edit=name):
                document = self.document()
                edit(document)
                verdict = chat.verify_document(document)
                if name == "removed":
                    # The later message is still signed: only the start is lost.
                    self.assertEqual((verdict["valid"], verdict["from_start"]), (True, False))
                else:
                    self.assertFalse(verdict["valid"], verdict)

    def test_a_message_removed_from_the_middle_is_seen(self):
        chat.receive_chat_message(celaut_pb2.ChatMessage(
            client_id=self.client_id, conversation_id=1, body="thank you"))
        document = self.document()
        del document["entries"][1]
        verdict = chat.verify_document(document)
        self.assertFalse(verdict["valid"])
        self.assertIn("does not follow", verdict["reason"])

    def test_a_shared_service_is_in_the_document_and_verifies(self):
        message = celaut_pb2.ChatMessage(
            client_id=self.client_id, conversation_id=1,
            body=f"look\n{chat.service_fallback_line(_service())}")
        message.service.CopyFrom(_service())
        chat.receive_chat_message(message)
        document = self.document()
        self.assertEqual(document["entries"][-1]["service"],
                         {"hashes": [["0102", "aabb"]], "tags": ["hello", "a/b"]})
        self.assertTrue(chat.verify_document(document)["valid"])
        document["entries"][-1]["service"]["tags"][0] = "other"
        self.assertFalse(chat.verify_document(document)["valid"])

    def test_a_document_that_is_not_well_formed_is_not_valid_and_never_raises(self):
        for document in ({}, {"format": chat.EVIDENCE_FORMAT}, {"format": "x"},
                         {"format": chat.EVIDENCE_FORMAT, "node": "n", "client_ref": "c",
                          "conversation_id": 1, "entries": []},
                         {"format": chat.EVIDENCE_FORMAT, "node": "n", "client_ref": "c",
                          "conversation_id": 1, "entries": [{"sequence": "x"}]},
                         {"format": chat.EVIDENCE_FORMAT, "node": "n", "client_ref": "c",
                          "conversation_id": 1,
                          "entries": [{"sequence": 1, "conversation_id": 1, "ts": 1,
                                       "from_operator": False, "body": "b", "prev_hash": "p",
                                       "signature": "s", "service": {"hashes": [["zz", "1"]],
                                                                     "tags": []}}]}):
            with self.subTest(document=document):
                self.assertFalse(chat.verify_document(document)["valid"])

    def test_an_unsigned_message_cannot_be_exported(self):
        with patch.object(node_identity, "get_identity_mnemonic", return_value=None):
            chat.receive_chat_message(celaut_pb2.ChatMessage(
                client_id=self.client_id, conversation_id=1, body="no key"))
        with self.assertRaises(chat.ChatError):
            chat.export_conversation(self.ref)

    def test_the_commands_write_and_check_a_file(self):
        import io
        from contextlib import redirect_stdout
        from src.commands import chat as commands
        out = io.StringIO()
        with patch("src.commands.chat._sync_quietly"), redirect_stdout(out):
            self.assertTrue(commands.export_thread(self.ref))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(out.getvalue())
        self.addCleanup(os.unlink, f.name)
        checked = io.StringIO()
        with redirect_stdout(checked):
            self.assertTrue(commands.verify_thread(f.name))
        self.assertIn("Valid: 2 message(s)", checked.getvalue())

        document = json.loads(out.getvalue())
        document["entries"][0]["body"] = "edited"
        with open(f.name, "w") as handle:
            json.dump(document, handle)
        rejected = io.StringIO()
        with redirect_stdout(rejected):
            self.assertFalse(commands.verify_thread(f.name))
        self.assertIn("NOT VALID", rejected.getvalue())


if __name__ == "__main__":
    unittest.main()

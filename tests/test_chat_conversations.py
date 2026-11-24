"""Chat conversations: one kind of conversation, named ``client_id#number``.

A conversation is always opened by a client against a node and lives on that node. The
node gives the number. The tests pin: the node that serves (numbers, who may write where,
the cap, the answer of the operator), the binding of a client to a peer (it changes
nothing in the conversations), the mirror that a node keeps of the conversations that it
opened as a client of a peer, and the gateway wiring of `Chat` and `GetChatThread`.
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


class _Database(unittest.TestCase):
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

    def tearDown(self):
        SQLConnection._connection = self._orig_conn
        self.conn.close()
        os.unlink(self.db_path)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServedConversationTests(_Database):
    """The node that serves: clients write, the operator answers."""

    def setUp(self):
        super().setUp()
        self.client_id = uuid4().hex
        self.sc.add_client(client_id=self.client_id, balance_mu=0, last_usage=None)
        self.other_id = uuid4().hex
        self.sc.add_client(client_id=self.other_id, balance_mu=0, last_usage=None)

    def send(self, client_id, body="help", conversation_id=0, **fields):
        return chat.receive_chat_message(celaut_pb2.ChatMessage(
            client_id=client_id, conversation_id=conversation_id, body=body, **fields))

    def test_conversation_zero_opens_the_next_number_of_that_client(self):
        self.assertEqual(self.send(self.client_id, "first"), (1, 1))
        self.assertEqual(self.send(self.client_id, "second topic"), (2, 2))
        # The numbers belong to the client: another client starts at 1 as well.
        self.assertEqual(self.send(self.other_id, "mine"), (1, 1))

    def test_a_message_goes_into_an_existing_conversation(self):
        self.send(self.client_id, "a")
        self.assertEqual(self.send(self.client_id, "b", conversation_id=1), (1, 2))
        history = chat.get_conversation_history(f"{self.client_id}#1")
        self.assertEqual([m["body"] for m in history], ["a", "b"])

    def test_the_sequence_is_one_cursor_over_all_the_conversations_of_a_client(self):
        self.send(self.client_id, "a")                       # #1 seq 1
        self.send(self.client_id, "b")                       # #2 seq 2
        self.send(self.client_id, "c", conversation_id=1)    # #1 seq 3
        thread = chat.get_client_thread(self.client_id)
        self.assertEqual([(e.sequence, e.conversation_id, e.body) for e in thread.entries],
                         [(1, 1, "a"), (2, 2, "b"), (3, 1, "c")])
        self.assertEqual(thread.latest, 3)
        later = chat.get_client_thread(self.client_id, after_sequence=2)
        self.assertEqual([e.body for e in later.entries], ["c"])

    def test_the_topic_is_kept_only_when_the_message_opens_the_conversation(self):
        self.send(self.client_id, "a", topic="instance down")
        self.send(self.client_id, "b", conversation_id=1, topic="ignored")
        [row] = chat.list_conversations()
        self.assertEqual(row["topic"], "instance down")
        with self.assertRaises(chat.ChatError):
            self.send(self.client_id, "c", topic="x" * (chat.MAX_TOPIC_CHARS + 1))

    def test_a_client_cannot_write_in_a_conversation_it_does_not_have(self):
        self.send(self.client_id, "mine")
        with self.assertRaises(chat.ChatError):
            self.send(self.other_id, "intruder", conversation_id=1)
        with self.assertRaises(chat.ChatError):
            self.send(self.client_id, "nothing there", conversation_id=9)
        self.assertEqual(chat.get_client_thread(self.other_id).latest, 0)

    def test_a_refused_message_opens_no_conversation(self):
        with self.assertRaises(chat.ChatError):
            self.send(uuid4().hex)
        with self.assertRaises(chat.ChatError):
            self.send(self.client_id, "")
        with self.assertRaises(chat.ChatError):
            self.send(self.client_id, "x" * (chat.MAX_MESSAGE_BYTES + 1))
        self.assertEqual(chat.list_conversations(), [])

    def test_a_client_reads_only_its_own_messages(self):
        self.send(self.client_id, "mine")
        self.send(self.other_id, "theirs")
        self.assertEqual([e.body for e in chat.get_client_thread(self.client_id).entries], ["mine"])
        self.assertEqual([e.body for e in chat.get_client_thread(self.other_id).entries], ["theirs"])

    def test_the_operator_answers_in_the_conversation_and_the_client_reads_it(self):
        self.send(self.client_id, "question")
        chat.reply(f"{self.client_id}#1", "answer")

        thread = chat.get_client_thread(self.client_id)
        self.assertEqual([(e.sequence, e.from_operator, e.body) for e in thread.entries],
                         [(1, False, "question"), (2, True, "answer")])
        [row] = chat.list_conversations()
        self.assertTrue(row["last_from_operator"], "answered: it does not wait")
        self.assertFalse(row["opened_by_us"])

    def test_a_conversation_waits_while_the_client_wrote_last(self):
        self.send(self.client_id, "question")
        [row] = chat.list_conversations()
        self.assertFalse(row["last_from_operator"])

    def test_the_operator_cannot_answer_what_does_not_exist_or_is_closed(self):
        with self.assertRaises(chat.ChatError):
            chat.reply(f"{uuid4().hex}#1", "hi")
        self.send(self.client_id, "q")
        with self.assertRaises(chat.ChatError):
            chat.reply(f"{self.client_id}#1", "")
        chat.close_conversation(f"{self.client_id}#1")
        with self.assertRaises(chat.ChatError):
            chat.reply(f"{self.client_id}#1", "too late")
        chat.reopen_conversation(f"{self.client_id}#1")
        chat.reply(f"{self.client_id}#1", "now yes")

    def test_a_message_of_the_client_opens_a_closed_conversation_again(self):
        self.send(self.client_id, "q")
        chat.close_conversation(f"{self.client_id}#1")
        self.send(self.client_id, "still here", conversation_id=1)
        [row] = chat.list_conversations()
        self.assertIsNone(row["closed_at"])

    def test_an_answer_has_a_cap_per_reply(self):
        with patch.object(chat, "MAX_THREAD_ENTRIES_PER_ANSWER", 2):
            for n in range(5):
                self.send(self.client_id, f"m{n}")
            thread = chat.get_client_thread(self.client_id)
            self.assertEqual([e.sequence for e in thread.entries], [1, 2])
            self.assertEqual(thread.latest, 5)
            more = chat.get_client_thread(self.client_id, after_sequence=2)
            self.assertEqual([e.sequence for e in more.entries], [3, 4])

    def test_the_cap_is_for_the_whole_client_not_for_each_conversation(self):
        with patch.object(chat, "MAX_STORED_MESSAGES_PER_CLIENT", 3):
            for n in range(4):
                self.send(self.client_id, f"m{n}")           # four conversations
            self.send(self.other_id, "other")
        thread = chat.get_client_thread(self.client_id)
        self.assertEqual([e.sequence for e in thread.entries], [2, 3, 4])
        self.assertEqual(thread.latest, 4)
        self.assertEqual(chat.get_client_thread(self.other_id).latest, 1)

    def test_a_number_is_never_given_twice_even_when_the_cut_removed_its_messages(self):
        with patch.object(chat, "MAX_STORED_MESSAGES_PER_CLIENT", 1):
            self.assertEqual(self.send(self.client_id, "a")[0], 1)
            self.assertEqual(self.send(self.client_id, "b")[0], 2)
            self.assertEqual(self.send(self.client_id, "c")[0], 3)
        numbers = [row["number"] for row in chat.list_conversations()]
        self.assertEqual(numbers, [3], "the conversations with no message left are removed")

    def test_the_refs(self):
        self.assertEqual(chat.parse_ref("abc#12"), ("abc", 12))
        for bad in ("", "abc", "abc#", "#1", "abc#x", "abc#0", "abc#-1"):
            with self.assertRaises(chat.ChatError, msg=bad):
                chat.parse_ref(bad)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AssociationTests(_Database):
    """Binding a client to a peer groups its conversations; it does not change them."""

    def setUp(self):
        super().setUp()
        self.client_id = uuid4().hex
        self.sc.add_client(client_id=self.client_id, balance_mu=0, last_usage=None)
        self.peer_id = "b" * 66
        self.sc.add_peer(peer_id=self.peer_id, advertisement=b"")

    def send(self, body, conversation_id=0):
        return chat.receive_chat_message(celaut_pb2.ChatMessage(
            client_id=self.client_id, conversation_id=conversation_id, body=body))

    def rows(self, table):
        return [tuple(r) for r in self.conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]

    def test_a_client_with_no_peer_is_listed_with_no_peer(self):
        self.send("hello")
        [row] = chat.list_conversations()
        self.assertIsNone(row["peer_id"])
        self.assertEqual(row["ref"], f"{self.client_id}#1")

    def test_the_association_leaves_every_conversation_and_message_as_it_was(self):
        self.send("one")
        self.send("two")
        self.send("one again", conversation_id=1)
        messages, conversations = self.rows("chat_messages"), self.rows("chat_conversations")

        self.sc.set_peer_local_client(peer_id=self.peer_id, client_id=self.client_id)

        self.assertEqual(self.rows("chat_messages"), messages)
        self.assertEqual(self.rows("chat_conversations"), conversations)
        listed = chat.list_conversations(peer_id=self.peer_id)
        self.assertEqual(sorted(r["number"] for r in listed), [1, 2])
        self.assertTrue(all(r["peer_id"] == self.peer_id for r in listed))
        self.assertEqual(chat.list_conversations(peer_id="c" * 66), [])

    def test_the_conversation_goes_on_after_the_association(self):
        self.send("before")
        self.sc.set_peer_local_client(peer_id=self.peer_id, client_id=self.client_id)

        self.assertEqual(self.send("after", conversation_id=1), (1, 2))
        chat.reply(f"{self.client_id}#1", "the operator answers the same conversation")
        thread = chat.get_client_thread(self.client_id)
        self.assertEqual([e.body for e in thread.entries],
                         ["before", "after", "the operator answers the same conversation"])


class _Remote:
    """What a peer node answers: the messages of one client and the numbers it gives."""

    def __init__(self, client_id):
        self.client_id = client_id
        self.entries = []     # (sequence, conversation, from_operator, body)
        self.sent = []

    def add(self, conversation, from_operator, body):
        self.entries.append((len(self.entries) + 1, conversation, from_operator, body))

    def chat(self, _channel, message):
        self.sent.append(message)
        number = message.conversation_id or (
            1 + max([c for _s, c, _o, _b in self.entries] or [0]))
        self.add(number, False, message.body)
        return celaut_pb2.ChatAck(stored=True, conversation_id=number,
                                  sequence=len(self.entries))

    def get_chat_thread(self, _channel, client_id, after_sequence=0, **_kwargs):
        assert client_id == self.client_id
        thread = celaut_pb2.ChatThread(latest=len(self.entries))
        for sequence, conversation, from_operator, body in self.entries:
            if sequence > after_sequence:
                thread.entries.add(sequence=sequence, conversation_id=conversation, ts=100 + sequence,
                                   from_operator=from_operator, body=body)
        return thread


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MirrorTests(_Database):
    """The conversations that this node opened as a client of a peer."""

    def setUp(self):
        super().setUp()
        self.peer_id = "a" * 66
        self.sc.add_peer(peer_id=self.peer_id, advertisement=b"")
        self.remote_client = uuid4().hex
        self.remote = _Remote(self.remote_client)
        patches = [
            patch.object(chat.sc, "get_peer_client", return_value=self.remote_client),
            patch.object(chat, "peer_channel", return_value=None),
            patch.object(chat.BeeClient, "chat", side_effect=self.remote.chat),
            patch.object(chat.BeeClient, "get_chat_thread", side_effect=self.remote.get_chat_thread),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_opening_sends_conversation_zero_and_mirrors_what_the_peer_numbered(self):
        ref = chat.open_conversation(self.peer_id, topic="payment", body="my payment is lost")

        self.assertEqual(ref, f"{self.remote_client}#1")
        self.assertEqual(self.remote.sent[0].conversation_id, 0)
        self.assertEqual(self.remote.sent[0].topic, "payment")
        [row] = chat.list_conversations()
        self.assertEqual((row["ref"], row["peer_id"], row["opened_by_us"], row["topic"], row["messages"]),
                         (ref, self.peer_id, True, "payment", 1))
        [message] = chat.get_conversation_history(ref)
        self.assertEqual((message["sequence"], message["from_operator"], message["body"]),
                         (1, False, "my payment is lost"))

    def test_the_answers_of_the_peer_arrive_with_the_sync(self):
        ref = chat.open_conversation(self.peer_id, topic="t", body="question")
        self.remote.add(1, True, "answer one")
        self.remote.add(1, True, "answer two")

        self.assertEqual(chat.sync_all(), 2)
        self.assertEqual(chat.sync_all(), 0, "nothing new, nothing copied")
        history = chat.get_conversation_history(ref)
        self.assertEqual([(m["sequence"], m["from_operator"], m["body"]) for m in history],
                         [(1, False, "question"), (2, True, "answer one"), (3, True, "answer two")])
        [row] = chat.list_conversations()
        self.assertTrue(row["last_from_operator"])

    def test_a_reply_goes_to_the_peer_in_the_same_conversation_and_is_mirrored(self):
        ref = chat.open_conversation(self.peer_id, topic="t", body="one")
        self.remote.add(1, True, "answer")

        chat.reply(ref, "two")

        self.assertEqual(self.remote.sent[-1].conversation_id, 1)
        self.assertEqual(self.remote.sent[-1].client_id, self.remote_client)
        self.assertEqual([m["body"] for m in chat.get_conversation_history(ref)],
                         ["one", "answer", "two"], "the answer is not skipped by the cursor")

    def test_a_mirror_never_skips_a_message_that_came_before_ours(self):
        ref = chat.open_conversation(self.peer_id, topic="t", body="one")
        self.remote.add(1, True, "answer, not read yet")
        chat.reply(ref, "two")
        self.assertEqual([m["sequence"] for m in chat.get_conversation_history(ref)], [1, 2, 3])

    def test_a_conversation_that_this_node_does_not_know_is_created_by_the_sync(self):
        chat.open_conversation(self.peer_id, topic="t", body="one")
        self.remote.add(2, False, "opened from another place with the same client")

        chat.sync_all()
        self.assertEqual(sorted(r["number"] for r in chat.list_conversations()), [1, 2])

    def test_a_closed_mirror_is_not_written_to(self):
        ref = chat.open_conversation(self.peer_id, topic="t", body="one")
        chat.close_conversation(ref)
        with self.assertRaises(chat.ChatError):
            chat.reply(ref, "two")

    def test_a_refusal_of_the_peer_is_the_error_and_nothing_is_mirrored(self):
        with patch.object(chat.BeeClient, "chat",
                          return_value=celaut_pb2.ChatAck(stored=False, reason="Unknown client_id.")):
            with self.assertRaises(chat.ChatError) as caught:
                chat.open_conversation(self.peer_id, topic="t", body="one")
        self.assertIn("Unknown client_id.", str(caught.exception))
        self.assertEqual(chat.list_conversations(), [])

    def test_a_failed_read_after_the_send_is_not_an_error_of_the_send(self):
        with patch.object(chat.BeeClient, "get_chat_thread", side_effect=RuntimeError("down")):
            ref = chat.open_conversation(self.peer_id, topic="t", body="one")
        self.assertEqual(ref, f"{self.remote_client}#1")
        self.assertEqual(chat.sync_all(), 1, "the next sync brings the message")

    def test_a_peer_that_is_down_does_not_stop_the_sync_of_the_others(self):
        other_peer, other_client = "c" * 66, uuid4().hex
        self.sc.add_peer(peer_id=other_peer, advertisement=b"")
        self.sc.open_chat_conversation(client_id=other_client, topic="x", remote_peer_id=other_peer, number=1)
        chat.open_conversation(self.peer_id, topic="t", body="one")
        self.remote.add(1, True, "answer")

        real = self.remote.get_chat_thread
        def thread(channel, client_id, after_sequence=0, **kw):
            if client_id == other_client:
                raise RuntimeError("down")
            return real(channel, client_id, after_sequence)
        with patch.object(chat.BeeClient, "get_chat_thread", side_effect=thread):
            self.assertEqual(chat.sync_all(), 1)

    def test_the_sync_can_be_for_one_peer(self):
        chat.open_conversation(self.peer_id, topic="t", body="one")
        self.remote.add(1, True, "answer")
        self.assertEqual(chat.sync_all(peer_id="d" * 66), 0)
        self.assertEqual(chat.sync_all(peer_id=self.peer_id), 1)

    def test_a_peer_cannot_make_this_node_store_what_a_message_may_not_hold(self):
        chat.open_conversation(self.peer_id, topic="t", body="one")
        self.remote.add(1, True, "x" * (chat.MAX_MESSAGE_BYTES * 2))
        self.remote.add(0, True, "a conversation 0 is not valid")
        chat.sync_all()
        history = chat.get_conversation_history(f"{self.remote_client}#1")
        self.assertEqual(len(history[-1]["body"].encode()), chat.MAX_MESSAGE_BYTES)
        self.assertEqual(len(history), 2)

    def test_the_two_kinds_are_listed_together_under_the_peer(self):
        client_id = uuid4().hex
        self.sc.add_client(client_id=client_id, balance_mu=0, last_usage=None)
        self.sc.set_peer_local_client(peer_id=self.peer_id, client_id=client_id)
        chat.receive_chat_message(celaut_pb2.ChatMessage(client_id=client_id, body="from the peer"))
        chat.open_conversation(self.peer_id, topic="t", body="to the peer")

        both = chat.list_conversations(peer_id=self.peer_id)
        self.assertEqual(sorted(r["opened_by_us"] for r in both), [False, True])
        self.assertEqual([r["opened_by_us"] for r in chat.list_conversations(opened_by_us=True)], [True])

    def test_reply_picks_the_kind_from_the_conversation(self):
        client_id = uuid4().hex
        self.sc.add_client(client_id=client_id, balance_mu=0, last_usage=None)
        chat.receive_chat_message(celaut_pb2.ChatMessage(client_id=client_id, body="q"))
        ref = chat.open_conversation(self.peer_id, topic="t", body="one")
        sent_before = len(self.remote.sent)

        chat.reply(f"{client_id}#1", "operator answer")
        self.assertEqual(len(self.remote.sent), sent_before, "a served conversation sends nothing")
        chat.reply(ref, "to the peer")
        self.assertEqual(len(self.remote.sent), sent_before + 1)

    def test_a_peer_that_is_not_known_cannot_be_written_to(self):
        with self.assertRaises(chat.ChatError):
            chat.open_conversation("f" * 66, topic="t", body="one")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SyncTickTests(unittest.TestCase):
    def test_the_tick_gates_itself_on_the_interval_and_never_raises(self):
        with patch.object(chat, "_last_sync", 0.0), \
                patch.object(chat, "sync_all", side_effect=RuntimeError("boom")) as sync:
            chat.sync_tick()
            chat.sync_tick()
        self.assertEqual(sync.call_count, 1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SchemaTests(unittest.TestCase):
    def test_the_tables_and_the_view_are_created_on_an_old_database(self):
        connection = sqlite3.connect(":memory:")
        try:
            cursor = connection.cursor()
            cursor.execute("CREATE TABLE peer (id TEXT PRIMARY KEY, local_client_id TEXT)")
            migrate.ensure_tables(cursor, TRACEABILITY_TABLES)
            names = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master")}
            for name in ("chat_conversations", "chat_messages", "chat_threads"):
                self.assertIn(name, names)
        finally:
            connection.close()

    def test_the_old_chat_tables_are_retired(self):
        connection = sqlite3.connect(":memory:")
        try:
            cursor = connection.cursor()
            for name in ("peer_chat_messages", "client_chat_messages", "peer_chat_conversations"):
                cursor.execute(f"CREATE TABLE {name} (x)")
            migrate.retire_peer_chat_tables(cursor)
            left = {r[0] for r in cursor.execute("SELECT name FROM sqlite_master")}
            self.assertFalse(left & {"peer_chat_messages", "client_chat_messages", "peer_chat_conversations"})
        finally:
            connection.close()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChatGatewayTests(unittest.TestCase):

    def chat_call(self, message, receive):
        with patch.object(gateway_module.BeeClient, "parse_one", return_value=message), \
                patch.object(gateway_module, "receive_chat_message", **receive), \
                patch.object(gateway_module.BeeClient, "respond",
                             side_effect=lambda message_iterator, indices=None: iter([message_iterator])):
            return list(gateway_module.Gateway().Chat(iter(()), _Context()))

    def test_the_ack_carries_the_conversation_and_the_sequence(self):
        [ack] = self.chat_call(celaut_pb2.ChatMessage(client_id="c1", body="hi"),
                               {"return_value": (4, 9)})
        self.assertEqual((ack.stored, ack.conversation_id, ack.sequence), (True, 4, 9))

    def test_a_refusal_is_an_ack_with_the_reason(self):
        [ack] = self.chat_call(celaut_pb2.ChatMessage(client_id="c1", body="hi"),
                               {"side_effect": chat.ChatError("Unknown conversation.")})
        self.assertEqual((ack.stored, ack.reason, ack.conversation_id, ack.sequence),
                         (False, "Unknown conversation.", 0, 0))

    def test_the_rpc_table(self):
        rpc = GATEWAY_RPCS["Chat"]
        self.assertEqual(rpc.input, {1: celaut_pb2.ChatMessage})
        self.assertEqual(rpc.output, {1: celaut_pb2.ChatAck})
        self.assertEqual(rpc.auth, "client-in-message")


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

    def test_a_client_reads_its_own_messages_from_the_cursor(self):
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

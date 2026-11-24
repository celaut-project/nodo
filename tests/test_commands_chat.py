"""``src.commands.chat``: what the commands print and which part of the manager they call.

The storage and the network are `src.manager.chat` (see `test_chat_conversations.py`).
This pins the CLI: a conversation is `client_id#number`, `chat_open` takes an optional
`--message` apart from the topic, and a read shows the sides the right way round.
"""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tests.config_bootstrap import load_example_config
load_example_config()

from src.commands import chat as commands  # noqa: E402


def run(function, *args, **kwargs):
    out = io.StringIO()
    with redirect_stdout(out):
        ok = function(*args, **kwargs)
    return ok, out.getvalue()


class OpenThreadTests(unittest.TestCase):
    @patch("src.manager.chat.open_conversation", return_value="c1#1")
    def test_no_message_sends_the_topic_itself(self, mock_open):
        ok, out = run(commands.open_thread, peer_id="peer-1", topic="status update")

        self.assertTrue(ok)
        mock_open.assert_called_once_with(
            peer_id="peer-1", topic="status update", body="status update", service=None)
        self.assertIn("c1#1", out)

    @patch("src.manager.chat.open_conversation", return_value="c1#1")
    def test_an_explicit_message_is_sent_instead_of_the_topic(self, mock_open):
        run(commands.open_thread, peer_id="peer-1", topic="status update", body="all green")
        mock_open.assert_called_once_with(
            peer_id="peer-1", topic="status update", body="all green", service=None)

    @patch("src.manager.chat.open_conversation", return_value="c1#1")
    def test_a_shared_service_travels_with_the_first_message(self, mock_open):
        run(commands.open_thread, peer_id="peer-1", topic="try this", service="hello-world")
        mock_open.assert_called_once_with(
            peer_id="peer-1", topic="try this", body="try this", service="hello-world")

    @patch("src.manager.chat.open_conversation", return_value="c1#7")
    def test_json_gives_the_parts_of_the_name(self, _open):
        ok, out = run(commands.open_thread, peer_id="peer-1", topic="t", as_json=True)
        self.assertTrue(ok)
        document = json.loads(out)
        document.pop("read_at", None)   # added to every JSON document by `emit_json`
        self.assertEqual(document, {
            "ref": "c1#7", "client_id": "c1", "number": 7, "peer_id": "peer-1", "topic": "t"})

    @patch("src.manager.chat.open_conversation", side_effect=Exception("unreachable"))
    def test_a_failure_is_reported_and_is_not_ok(self, _open):
        ok, out = run(commands.open_thread, peer_id="peer-1", topic="t")
        self.assertFalse(ok)
        self.assertIn("Could not reach peer peer-1", out)


class ReplyTests(unittest.TestCase):
    @patch("src.manager.chat.reply")
    def test_the_ref_is_passed_as_it_is(self, mock_reply):
        ok, _ = run(commands.reply_in_thread, "c1#3", "hello", service="s")
        self.assertTrue(ok)
        mock_reply.assert_called_once_with(ref="c1#3", body="hello", service="s")

    def test_a_refusal_prints_stop(self):
        from src.manager.chat import ChatError
        with patch("src.manager.chat.reply", side_effect=ChatError("closed")):
            ok, out = run(commands.reply_in_thread, "c1#3", "hello")
        self.assertFalse(ok)
        self.assertIn("STOP: closed", out)


def history(*rows):
    return [{"sequence": i + 1, "from_operator": op, "body": body, "ts": 0, "service": None}
            for i, (op, body) in enumerate(rows)]


class ShowThreadTests(unittest.TestCase):
    def show(self, conversation, rows):
        with patch("src.manager.chat.get_conversation", return_value=conversation), \
                patch("src.manager.chat.get_conversation_history", return_value=history(*rows)), \
                patch("src.commands.chat._sync_quietly"):
            return run(commands.show_thread, "c1#1")[1]

    def test_on_a_served_conversation_the_operator_is_us(self):
        out = self.show({"opened_by_us": False, "peer_id": None}, [(False, "help"), (True, "done")])
        lines = out.strip().splitlines()
        self.assertIn("them: help", lines[0])
        self.assertIn("us: done", lines[1])

    def test_on_a_mirror_the_client_is_us(self):
        out = self.show({"opened_by_us": True, "peer_id": "p"}, [(False, "help"), (True, "done")])
        lines = out.strip().splitlines()
        self.assertIn("us: help", lines[0])
        self.assertIn("them: done", lines[1])

    def test_a_mirror_is_synced_before_it_is_read(self):
        with patch("src.manager.chat.get_conversation",
                   return_value={"opened_by_us": True, "peer_id": "p"}), \
                patch("src.manager.chat.get_conversation_history", return_value=[]), \
                patch("src.commands.chat._sync_quietly") as sync:
            run(commands.show_thread, "c1#1")
        sync.assert_called_once_with(peer_id="p")


class ListThreadsTests(unittest.TestCase):
    def test_it_marks_what_waits_and_who_opened_it(self):
        rows = [
            {"ref": "c1#1", "peer_id": None, "opened_by_us": False, "topic": "down",
             "closed_at": None, "messages": 1, "last_ts": 0, "last_from_operator": False},
            {"ref": "c2#1", "peer_id": "peer-1", "opened_by_us": True, "topic": None,
             "closed_at": "2026-01-01", "messages": 2, "last_ts": 0, "last_from_operator": True},
        ]
        with patch("src.manager.chat.list_conversations", return_value=rows), \
                patch("src.commands.chat._sync_quietly"):
            ok, out = run(commands.list_threads)
        self.assertTrue(ok)
        first, second = out.strip().splitlines()
        self.assertIn("c1#1 [open, waiting] a client's, no peer", first)
        self.assertIn("-- down", first)
        self.assertIn("c2#1 [closed] ours", second)
        self.assertIn("peer-1", second)


if __name__ == "__main__":
    unittest.main()

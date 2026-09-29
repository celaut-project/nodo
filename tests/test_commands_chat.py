"""``src.commands.chat.open_thread``: topic label vs. first-message body.

The TUI's peer/topic/body wizard needs a real message distinct from the topic
label; `--message` (`nodo chat_open <peer> <topic> [--message body]`) is
additive, so this pins both the new and the pre-existing shape in one place
rather than duplicating `ConversationTests` (`tests/test_peer_chat.py`), which
already covers `open_conversation` itself.
"""
import unittest
from unittest.mock import patch

from src.commands.chat import open_thread


class OpenThreadTests(unittest.TestCase):
    @patch("src.manager.chat.send_chat_message")
    @patch("src.manager.chat.open_conversation", return_value="conv-1")
    def test_no_message_sends_topic_itself(self, mock_open, mock_send):
        """Unchanged from before `--message` existed: no explicit body, so the
        topic doubles as the opening message."""
        ok = open_thread(peer_id="peer-1", topic="status update")

        self.assertTrue(ok)
        mock_send.assert_called_once_with(
            peer_id="peer-1", body="status update", conversation_id="conv-1",
        )

    @patch("src.manager.chat.send_chat_message")
    @patch("src.manager.chat.open_conversation", return_value="conv-1")
    def test_explicit_message_is_sent_instead_of_topic(self, mock_open, mock_send):
        ok = open_thread(peer_id="peer-1", topic="status update", body="all green")

        self.assertTrue(ok)
        mock_send.assert_called_once_with(
            peer_id="peer-1", body="all green", conversation_id="conv-1",
        )

    @patch("src.manager.chat.close_conversation")
    @patch("src.manager.chat.send_chat_message", side_effect=Exception("unreachable"))
    @patch("src.manager.chat.open_conversation", return_value="conv-1")
    def test_a_send_failure_closes_the_thread_it_just_opened(self, mock_open, mock_send, mock_close):
        ok = open_thread(peer_id="peer-1", topic="status update", body="all green")

        self.assertFalse(ok)
        mock_close.assert_called_once_with("conv-1")


if __name__ == "__main__":
    unittest.main()

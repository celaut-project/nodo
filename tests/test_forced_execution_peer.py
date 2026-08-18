"""Regression tests for the ``forced_execution_peer`` hint table (issue #234).

`nodo force_execution <peer_id> <service>` needs to tell `launch_service`,
server-side, which peer to delegate straight to -- without touching the wire
protocol (a `Configuration` field would get forwarded verbatim to the peer it
delegates to, whose own peer-id namespace is unrelated to ours) and without
keying on the client id (drawn from a small reusable pool, so a hint keyed on
it could leak onto a later, unrelated `nodo execute` call). Instead it's
correlated via a fresh, single-use token sent as this call's
`recursion_guard_token` and consumed (popped) exactly once.

The schema here comes from ``migrate.create_tables`` rather than a copy of the
DDL, so these tests fail again if the table and its accessors ever diverge.
"""

import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ForcedExecutionPeerTests(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._original_connection = SQLConnection._connection

        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original_connection

    def test_a_stored_hint_is_read_back(self):
        self.sc.set_forced_execution_peer(token="tok-1", peer_id="peer-a")
        self.assertEqual(self.sc.pop_forced_execution_peer(token="tok-1"), "peer-a")

    def test_popping_consumes_it_so_it_cannot_be_read_twice(self):
        # The whole point: a second launch_service call correlated with the
        # same token (which shouldn't happen, but must not be exploitable)
        # must not see a stale hint.
        self.sc.set_forced_execution_peer(token="tok-1", peer_id="peer-a")
        self.sc.pop_forced_execution_peer(token="tok-1")
        self.assertIsNone(self.sc.pop_forced_execution_peer(token="tok-1"))

    def test_an_unknown_token_returns_none(self):
        self.assertIsNone(self.sc.pop_forced_execution_peer(token="never-set"))

    def test_setting_a_token_again_replaces_the_previous_hint(self):
        self.sc.set_forced_execution_peer(token="tok-1", peer_id="peer-a")
        self.sc.set_forced_execution_peer(token="tok-1", peer_id="peer-b")
        self.assertEqual(self.sc.pop_forced_execution_peer(token="tok-1"), "peer-b")

    def test_two_tokens_do_not_interfere(self):
        # Two concurrent force_execution calls (different peers) must not
        # cross-contaminate -- each token is its own row.
        self.sc.set_forced_execution_peer(token="tok-1", peer_id="peer-a")
        self.sc.set_forced_execution_peer(token="tok-2", peer_id="peer-b")
        self.assertEqual(self.sc.pop_forced_execution_peer(token="tok-2"), "peer-b")
        self.assertEqual(self.sc.pop_forced_execution_peer(token="tok-1"), "peer-a")


if __name__ == "__main__":
    unittest.main()

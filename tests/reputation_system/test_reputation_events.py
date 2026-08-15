"""A score and the events that explain it, against real SQLite.

`peer.reputation_score` is a running total: -390 could be one catastrophe or forty
refused calls, and there was no way to tell. The events table answers that, but only
if it cannot drift from the total it explains -- so the update and the event are one
transaction, and `score_after` is written at the same moment.

These run the real statements (CHECK constraints, the upsert) against an in-memory
database built by the real migration, because that is where this kind of code fails:
a constraint that rejects a legitimate value, or an upsert that inserts twice.
"""
import sqlite3
import threading
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.reputation_system.reasons import Reason
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReputationEventTests(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_tables(self.connection.cursor())
        self.connection.commit()

        # Bypass __init__ so no singleton opens the node's real database file.
        self.sc = SQLConnection.__new__(SQLConnection)
        patches = [
            mock.patch.object(SQLConnection, "_connection", self.connection),
            mock.patch.object(SQLConnection, "_lock", threading.Lock()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.connection.close)

    def _peer(self, peer_id="peer-1", score=0, index=0):
        self.connection.execute(
            "INSERT INTO peer (id, reputation_score, reputation_index) VALUES (?, ?, ?)",
            (peer_id, score, index),
        )
        self.connection.commit()

    def test_a_peer_event_records_the_reason_and_the_total_it_produced(self):
        self._peer(score=10, index=1)

        self.assertTrue(self.sc.update_reputation_peer(
            "peer-1", -100, Reason.PAYMENT_UNACKNOWLEDGED
        ))

        events = self.sc.get_reputation_events("peer", "peer-1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["amount"], -100)
        self.assertEqual(events[0]["reason"], Reason.PAYMENT_UNACKNOWLEDGED)
        self.assertEqual(events[0]["score_after"], -90)

        row = self.connection.execute(
            "SELECT reputation_score, reputation_index FROM peer WHERE id = 'peer-1'"
        ).fetchone()
        self.assertEqual(row["reputation_score"], -90)
        self.assertEqual(row["reputation_index"], 2)

    def test_a_history_adds_up_to_the_score_it_explains(self):
        self._peer()
        for amount, reason in ((10, Reason.PAYMENT_COMMUNICATED),
                               (-1, Reason.PAYMENT_CALL_FAILED),
                               (-100, Reason.PEER_REFRESH_FAILED)):
            self.sc.update_reputation_peer("peer-1", amount, reason)

        events = self.sc.get_reputation_events("peer", "peer-1")
        self.assertEqual([e["amount"] for e in events], [-100, -1, 10])  # newest first
        row = self.connection.execute(
            "SELECT reputation_score FROM peer WHERE id = 'peer-1'"
        ).fetchone()
        self.assertEqual(sum(e["amount"] for e in events), row["reputation_score"])
        self.assertEqual(events[0]["score_after"], row["reputation_score"])

    def test_nothing_is_written_for_a_peer_that_does_not_exist(self):
        """The score has nowhere to go, so the event would explain nothing."""
        self.assertFalse(self.sc.update_reputation_peer(
            "peer-missing", -10, Reason.PEER_REFRESH_FAILED
        ))
        self.assertEqual(self.sc.get_reputation_events("peer", "peer-missing"), [])

    def test_a_service_accumulates_across_the_instances_that_ran_it(self):
        """The point of scoring the service: an instance is gone, its record is not."""
        self.sc.update_reputation_service("service-1", 10, Reason.INTERVAL_CHARGED)
        self.sc.update_reputation_service("service-1", -10, Reason.INSTANCE_OUT_OF_BALANCE)
        self.sc.update_reputation_service("service-1", -100, Reason.INSTANCE_LOST)

        self.assertEqual(self.sc.get_service_reputation("service-1"), -100)
        events = self.sc.get_reputation_events("service", "service-1")
        self.assertEqual(len(events), 3)
        rows = self.connection.execute(
            "SELECT reputation_index FROM service_reputation WHERE service_id = 'service-1'"
        ).fetchone()
        self.assertEqual(rows["reputation_index"], 3)

    def test_a_service_never_scored_has_no_score_rather_than_zero(self):
        self.assertIsNone(self.sc.get_service_reputation("service-unknown"))

    def test_a_peer_and_a_service_sharing_an_id_keep_separate_histories(self):
        self._peer(peer_id="same-id")
        self.sc.update_reputation_peer("same-id", 10, Reason.PAYMENT_COMMUNICATED)
        self.sc.update_reputation_service("same-id", -100, Reason.INSTANCE_LOST)

        self.assertEqual(len(self.sc.get_reputation_events("peer", "same-id")), 1)
        self.assertEqual(len(self.sc.get_reputation_events("service", "same-id")), 1)

    def test_the_history_stays_bounded_because_the_events_arrive_on_a_timer(self):
        """The maintenance tick scores every instance and every unreachable peer once
        per iteration -- 8 640 rows a day each at the default 10s. Unbounded, this
        table would outgrow the rest of the database and still say nothing useful."""
        self._peer()
        writes = SQLConnection.MAX_EVENTS_PER_SUBJECT + SQLConnection.PRUNE_EVENTS_EVERY * 3
        for _ in range(writes):
            self.sc.update_reputation_peer("peer-1", -100, Reason.PEER_REFRESH_FAILED)

        stored = self.connection.execute(
            "SELECT COUNT(*) FROM reputation_events WHERE subject_id = 'peer-1'"
        ).fetchone()[0]
        self.assertLessEqual(
            stored,
            SQLConnection.MAX_EVENTS_PER_SUBJECT + SQLConnection.PRUNE_EVENTS_EVERY,
        )

        # The running total is the long-term memory, and it counts every event, pruned
        # or not -- so bounding the history costs no history of the *score*.
        row = self.connection.execute(
            "SELECT reputation_score, reputation_index FROM peer WHERE id = 'peer-1'"
        ).fetchone()
        self.assertEqual(row["reputation_score"], -100 * writes)
        self.assertEqual(row["reputation_index"], writes)

        # And what survives is the newest, which is what a reader wants.
        newest = self.sc.get_reputation_events("peer", "peer-1", limit=1)[0]
        self.assertEqual(newest["score_after"], -100 * writes)

    def test_pruning_one_subject_leaves_the_others_alone(self):
        self._peer(peer_id="loud")
        self._peer(peer_id="quiet")
        self.sc.update_reputation_peer("quiet", -1, Reason.PAYMENT_CALL_FAILED)
        for _ in range(SQLConnection.MAX_EVENTS_PER_SUBJECT + SQLConnection.PRUNE_EVENTS_EVERY):
            self.sc.update_reputation_peer("loud", -1, Reason.PEER_REFRESH_FAILED)

        self.assertEqual(len(self.sc.get_reputation_events("peer", "quiet")), 1)

    def test_an_unknown_subject_kind_reads_nothing(self):
        self.assertEqual(self.sc.get_reputation_events("wallet", "whatever"), [])


if __name__ == "__main__":
    unittest.main()

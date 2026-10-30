"""A delegated child that the peer suspends is not charged to its father (F1 of the review of #595).

The peer suspends a child whose deposit there ran out (its price rose over the quote,
say), and the child still answers `Metrics` and its statement. The parent stopped a
delegated row only when the peer no longer knew the token, so it went on charging the
father every tick for an instance that did not run: 850 -> 650 in 5 ticks, 0 stops.
"""

import sqlite3
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()
    from protos import celaut_pb2
    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.manager import maintain, metrics
    from src.utils import dead_instances
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

PEER_TOKEN = "token-on-the-peer"
ALIAS = "alias-of-the-child"
FATHER = "father-client"


class _Database(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        self.addCleanup(self._restore)

    def _restore(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ParentFollowsThePeerTests(_Database):
    def setUp(self):
        super().setUp()
        self.sc.add_client(FATHER, 5000, None)
        self.sc.add_delegated_instance(
            father_id=FATHER, alias=ALIAS, external_token=PEER_TOKEN,
            peer_id="peer", serialized_instance=b"", service_id="s", balance_mu=850,
            peer_balance_mu=900, movements=(ledger.Movement(ledger.START, 850),),
            sent_peer_mu=900,
        )
        self.stopped = []

    def _tick(self, suspended, own_mu=40, grace=3600):
        def read(row, debug_mode=False):
            return 0, {**maintain._unmoved_state(row), "peer_suspended": suspended}

        with patch.object(maintain, "sc", self.sc), \
                patch.object(maintain, "_read_child", side_effect=read), \
                patch.object(maintain, "_check_our_account"), \
                patch.object(maintain, "charge_at_launch_price", return_value=own_mu), \
                patch.object(maintain, "system_scarcity", return_value={}), \
                patch.object(dead_instances, "suspended_grace_seconds", return_value=grace), \
                patch.object(maintain, "stop_instance",
                             side_effect=lambda token, reason: self.stopped.append((token, reason))):
            maintain.maintain_delegated_instances()

    @property
    def row(self):
        return self.sc.get_delegated_instances()[0]

    def test_while_the_peer_has_it_suspended_the_father_pays_nothing(self):
        self._tick(suspended=False)           # the first charge sets charged_until
        before = self.row["balance_mu"]
        for _ in range(5):
            self._tick(suspended=True)
        self.assertEqual(self.row["balance_mu"], before)
        self.assertIsNotNone(self.row["suspended_at"])
        self.assertEqual(self.stopped, [])

    def test_once_the_peer_runs_it_again_it_is_charged_again(self):
        self._tick(suspended=False)
        self._tick(suspended=True)
        self._tick(suspended=False)
        self.assertIsNone(self.row["suspended_at"])
        before = self.row["balance_mu"]
        self.sc._execute("UPDATE delegated_instances SET charged_until = charged_until - 10")
        self._tick(suspended=False)
        self.assertLess(self.row["balance_mu"], before)

    def test_it_is_stopped_with_a_refund_when_the_grace_here_ends(self):
        self._tick(suspended=True, grace=0)
        self.assertEqual(self.stopped, [(ALIAS, dead_instances.OUT_OF_BALANCE)])

    def test_a_peer_that_does_not_say_leaves_the_row_as_it_was(self):
        self._tick(suspended=True)
        self._tick(suspended=None)
        self.assertIsNotNone(self.row["suspended_at"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerSaysSoTests(_Database):
    def test_metrics_of_a_suspended_instance_say_it_is(self):
        self.sc.add_local_instance(
            father_id="father", container_ip="192.168.200.7", container_id="a", name="web",
            balance_mu=10, serialized_instance="x", service_id="svc", virtualizer="ch",
            disk_space=0, envs="{}")
        with patch.object(metrics, "sc", self.sc):
            self.assertFalse(metrics.get_metrics("a").suspended)
            self.sc.suspend_local_instance("a", dead_instances.OUT_OF_BALANCE)
            answer = metrics.get_metrics("a")
        self.assertTrue(answer.suspended)
        self.assertEqual(answer.balance.n, "10")

    def test_the_field_is_on_the_wire(self):
        wire = celaut_pb2.Metrics(suspended=True).SerializeToString()
        self.assertTrue(celaut_pb2.Metrics.FromString(wire).suspended)
        # A peer from before the field sends nothing, which reads as running.
        self.assertFalse(celaut_pb2.Metrics.FromString(b"").suspended)


if __name__ == "__main__":
    unittest.main()

"""An instance keeps what the node said about closing time, and gets that action (issue #344)."""
import sqlite3
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.manager import hibernation, maintain
    from src.database import ledger
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class InstanceKeepsClosingTests(unittest.TestCase):
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

    def _local(self, token, on_close=None, closes_at=None):
        self.sc.add_local_instance(
            father_id="father", container_ip=f"192.168.200.{ord(token)}", container_id=token,
            name=token, balance_mu=100, serialized_instance="x", service_id="svc",
            virtualizer="ch", mem_limit=1000, disk_space=500, cpu_period=100, cpu_quota=50,
            envs="{}", launch_on_close=on_close, launch_closes_at=closes_at,
        )

    def test_a_launch_writes_what_the_node_said(self):
        self._local("a", "hibernate", 1_800_000_000)
        self._local("b")
        terms = self.sc.get_local_closing_terms()
        self.assertEqual(terms["a"], ("hibernate", 1_800_000_000))
        self.assertEqual(terms["b"], (None, None))

    def test_a_delegated_start_writes_the_quote_of_the_peer(self):
        self.sc.add_client("father", 5000, None)
        self.sc.add_delegated_instance(
            father_id="father", alias="alias", external_token="tok", peer_id="peer",
            serialized_instance=b"", service_id="s", balance_mu=850, peer_balance_mu=900,
            movements=(ledger.Movement(ledger.START, 850),), sent_peer_mu=900,
            quote={"on_close": "stop", "closes_at": 1_800_000_000},
        )
        row = self.sc.get_delegated_instances()[0]
        self.assertEqual((row["quote_on_close"], row["quote_closes_at"]), ("stop", 1_800_000_000))

    def _closed(self, current, force=False, dev=()):
        return [
            mock.patch.object(maintain.activity_window, "is_open", return_value=False),
            mock.patch.object(maintain.activity_window, "on_close_policy", return_value=current),
            mock.patch.object(maintain.activity_window, "force_actual_on_close_policy",
                              return_value=force),
            mock.patch.object(maintain, "sc", self.sc),
            mock.patch.object(maintain, "descends_from_dev_client",
                              side_effect=lambda token: token in dev),
        ]

    def _enforce(self, current, **kwargs):
        patches = self._closed(current, **kwargs)
        for patch in patches:
            patch.start()
        try:
            with mock.patch.object(hibernation, "check_can_hibernate"), \
                    mock.patch.object(hibernation, "kill"), \
                    mock.patch.object(maintain, "stop_instance") as stop:
                maintain.enforce_activity_window()
            return stop
        finally:
            for patch in patches:
                patch.stop()

    def test_after_an_edit_of_on_close_the_instance_keeps_the_action_of_its_row(self):
        self._local("a", "refuse", 0)
        self._local("b", "stop", 0)
        self._local("c", "hibernate", 0)
        stop = self._enforce(current="stop")
        self.assertEqual([call.kwargs["token"] for call in stop.call_args_list], ["b"])
        self.assertTrue(self.sc.is_local_instance_hibernated(id="c"))
        self.assertFalse(self.sc.is_local_instance_hibernated(id="a"))

    def test_a_row_with_no_value_takes_the_current_policy(self):
        self._local("a")
        stop = self._enforce(current="stop")
        self.assertEqual([call.kwargs["token"] for call in stop.call_args_list], ["a"])

    def test_the_forced_setting_makes_the_current_policy_rule(self):
        self._local("a", "refuse", 0)
        self._local("b", "stop", 0)
        stop = self._enforce(current="hibernate", force=True)
        stop.assert_not_called()
        self.assertTrue(self.sc.is_local_instance_hibernated(id="a"))
        self.assertTrue(self.sc.is_local_instance_hibernated(id="b"))

    def test_closing_before_the_announced_time_is_logged_and_still_done(self):
        self._local("b", "stop", 4_000_000_000)
        with mock.patch.object(maintain, "_log_broken_commitment") as note:
            stop = self._enforce(current="refuse")
        note.assert_called_once()
        self.assertEqual(stop.call_count, 1)


if __name__ == "__main__":
    unittest.main()

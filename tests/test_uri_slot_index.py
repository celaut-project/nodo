"""The index from a published Uri to an instance and a slot (#669)."""

import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.database import migrate
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


def _instance(*pairs):
    """An Instance with one uri_slot for each (internal_port, ip, port)."""
    instance = celaut_pb2.Instance()
    for internal, ip, port in pairs:
        slot = instance.uri_slot.add()
        slot.internal_port = internal
        slot.uri.add(ip=ip, port=port)
    return instance.SerializeToString()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UriSlotIndexTests(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        migrate.create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        self._add("a")

    def tearDown(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original

    def _add(self, token, serialized=b"x"):
        self.sc.add_local_instance(
            father_id="father", container_ip="192.168.200.1", container_id=token, name=token,
            balance_mu=100, serialized_instance=serialized, service_id="svc", virtualizer="ch",
            mem_limit=1000, disk_space=500, cpu_period=100, cpu_quota=50, envs="{}",
        )

    def test_a_published_uri_finds_the_instance_and_the_internal_port(self):
        self.sc.set_local_instance_definition(
            "a", _instance((8333, "203.0.113.5", 18333), (8332, "203.0.113.5", 18332)))
        self.assertEqual(self.sc.find_instance_by_uri("203.0.113.5", 18333), ("a", 8333))
        self.assertEqual(self.sc.find_instance_by_uri("203.0.113.5", 18332), ("a", 8332))

    def test_an_unknown_uri_finds_nothing(self):
        self.sc.set_local_instance_definition("a", _instance((8333, "203.0.113.5", 18333)))
        self.assertIsNone(self.sc.find_instance_by_uri("203.0.113.5", 1))
        self.assertIsNone(self.sc.find_instance_by_uri("203.0.113.6", 18333))

    def test_a_new_definition_replaces_the_rows(self):
        self.sc.set_local_instance_definition("a", _instance((8333, "203.0.113.5", 18333)))
        self.sc.set_local_instance_definition("a", _instance((8333, "203.0.113.5", 19999)))
        self.assertIsNone(self.sc.find_instance_by_uri("203.0.113.5", 18333))
        self.assertEqual(self.sc.find_instance_by_uri("203.0.113.5", 19999), ("a", 8333))

    def test_a_definition_that_cannot_be_read_gives_no_rows(self):
        self.sc.set_local_instance_definition("a", b"\xff\xff\xff")
        self.assertIsNone(self.sc.find_instance_by_uri("203.0.113.5", 18333))

    def test_a_killed_instance_has_no_rows(self):
        self.sc.set_local_instance_definition("a", _instance((8333, "203.0.113.5", 18333)))
        self.sc.mark_local_killed("a", "test")
        self.assertIsNone(self.sc.find_instance_by_uri("203.0.113.5", 18333))
        self.assertEqual(self.sc._execute("SELECT COUNT(*) FROM uri_slot_index").fetchone()[0], 0)

    def test_a_hibernated_instance_keeps_its_rows(self):
        self.sc.set_local_instance_definition("a", _instance((8333, "203.0.113.5", 18333)))
        self.sc.hibernate_local_instance("a")
        self.assertEqual(self.sc.find_instance_by_uri("203.0.113.5", 18333), ("a", 8333))

    def test_the_migration_fills_the_index_from_the_live_rows(self):
        self._add("b", _instance((80, "10.0.0.2", 80)))
        self._add("c", _instance((81, "10.0.0.3", 81)))
        self.sc.mark_local_killed("c", "test")
        self.sc._execute("DELETE FROM uri_slot_index")
        migrate.backfill_uri_slot_index(SQLConnection._connection.cursor())
        migrate.backfill_uri_slot_index(SQLConnection._connection.cursor())  # idempotent
        self.assertEqual(self.sc.find_instance_by_uri("10.0.0.2", 80), ("b", 80))
        self.assertIsNone(self.sc.find_instance_by_uri("10.0.0.3", 81))


if __name__ == "__main__":
    unittest.main()

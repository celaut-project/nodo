"""
Tests for virtiofs shared-disk resource accounting and the network->origin
persistence that backs it.

Covers:
  * origin recorded on first create, NOT overwritten when another instance joins;
  * du of the shared dir attributed to the origin instance;
  * the origin's declared disk_space enforced as a hard ceiling (cap);
  * the network_origins DB accessors (record/get/list/delete) idempotency.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

IMPORT_ERROR = None
try:
    from src.database.sql_connection import SQLConnection
    from src.database.migrate import create_tables
    from src.manager.virtiofs_accounting import origin_instance_shared_disk_usage_bytes
    from src.virtualizers.ch import virtiofs as ch_virtiofs
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    SQLConnection = None  # type: ignore[assignment]


def _write_shared_file(base_dir: str, nid: str, name: str, size: int) -> None:
    d = ch_virtiofs.shared_dir(base_dir, nid)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"x" * size)


# NOTE: the pure attribution math (attributed_shared_disk_usage_bytes) is tested
# in test_ch_virtiofs.py, which loads virtiofs.py directly and so runs without
# the full CH/Docker runtime. The tests below need the real SQLConnection and
# therefore skip in a bare unit-test env (mirroring test_sql_connection_*).


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NetworkOriginDBTests(unittest.TestCase):
    """Exercise the real SQL against an in-memory DB (swap the singleton conn)."""

    def setUp(self):
        self._saved_conn = SQLConnection._connection
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        create_tables(conn.cursor())
        conn.commit()
        SQLConnection._connection = conn
        self.sc = SQLConnection()

    def tearDown(self):
        try:
            SQLConnection._connection.close()
        except Exception:
            pass
        SQLConnection._connection = self._saved_conn

    def test_record_first_wins_and_not_overwritten(self):
        self.assertTrue(
            self.sc.record_network_origin("net1", service_id="svcA", instance_id="vmA")
        )
        # A later join by a different instance must be ignored.
        self.assertFalse(
            self.sc.record_network_origin("net1", service_id="svcB", instance_id="vmB")
        )
        origin = self.sc.get_network_origin("net1")
        self.assertEqual(origin, {"service_id": "svcA", "instance_id": "vmA"})

    def test_get_origin_networks_lists_only_that_instance(self):
        self.sc.record_network_origin("net1", service_id="svcA", instance_id="vmA")
        self.sc.record_network_origin("net2", service_id="svcA", instance_id="vmA")
        self.sc.record_network_origin("net3", service_id="svcC", instance_id="vmC")
        self.assertEqual(sorted(self.sc.get_origin_networks("vmA")), ["net1", "net2"])
        self.assertEqual(self.sc.get_origin_networks("vmC"), ["net3"])
        self.assertEqual(self.sc.get_origin_networks("nobody"), [])

    def test_delete_network_origin(self):
        self.sc.record_network_origin("net1", service_id="svcA", instance_id="vmA")
        self.sc.delete_network_origin("net1")
        self.assertIsNone(self.sc.get_network_origin("net1"))
        # After deletion a fresh creator can claim origin.
        self.assertTrue(
            self.sc.record_network_origin("net1", service_id="svcZ", instance_id="vmZ")
        )
        self.assertEqual(self.sc.get_network_origin("net1")["service_id"], "svcZ")

    def test_missing_origin_is_none(self):
        self.assertIsNone(self.sc.get_network_origin("does-not-exist"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OriginInstanceAccountingTests(unittest.TestCase):
    """End-to-end: du of an instance's originated networks, capped by declared."""

    def setUp(self):
        self._saved_conn = SQLConnection._connection
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        create_tables(conn.cursor())
        conn.commit()
        SQLConnection._connection = conn
        self.sc = SQLConnection()
        self._tmp = tempfile.TemporaryDirectory()
        self.base = str(Path(self._tmp.name) / "virtiofs")

    def tearDown(self):
        self._tmp.cleanup()
        try:
            SQLConnection._connection.close()
        except Exception:
            pass
        SQLConnection._connection = self._saved_conn

    def _add_instance(self, instance_id, disk_space):
        self.sc.add_local_instance(
            father_id="client1",
            container_ip="10.0.0.2",
            container_id=instance_id,
            name=f"name-{instance_id}",
            gas=0,
            serialized_instance="{}",
            service_id="svcA",
            virtualizer="cloud_hypervisor",
            disk_space=disk_space,
        )

    def test_du_attributed_to_origin_instance(self):
        self._add_instance("vmA", disk_space=10_000)
        self.sc.record_network_origin("net1", service_id="svcA", instance_id="vmA")
        self.sc.record_network_origin("net2", service_id="svcA", instance_id="vmA")
        _write_shared_file(self.base, "net1", "a", 400)
        _write_shared_file(self.base, "net2", "b", 600)
        used = origin_instance_shared_disk_usage_bytes(
            "vmA", base_dir=self.base, sc=self.sc
        )
        self.assertEqual(used, 1_000)

    def test_declared_disk_space_is_the_cap(self):
        self._add_instance("vmA", disk_space=500)
        self.sc.record_network_origin("net1", service_id="svcA", instance_id="vmA")
        _write_shared_file(self.base, "net1", "a", 5_000)
        used = origin_instance_shared_disk_usage_bytes(
            "vmA", base_dir=self.base, sc=self.sc
        )
        self.assertEqual(used, 500)  # measured 5000, capped by declared 500

    def test_non_origin_instance_counts_nothing(self):
        self._add_instance("vmB", disk_space=10_000)
        # vmB joined net1 but is NOT its origin.
        self.sc.record_network_origin("net1", service_id="svcA", instance_id="vmA")
        _write_shared_file(self.base, "net1", "a", 4_000)
        used = origin_instance_shared_disk_usage_bytes(
            "vmB", base_dir=self.base, sc=self.sc
        )
        self.assertEqual(used, 0)


if __name__ == "__main__":
    unittest.main()

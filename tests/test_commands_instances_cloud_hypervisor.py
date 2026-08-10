import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.commands import instances as instances_cmd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    instances_cmd = None  # type: ignore[assignment]


def _serialized_instance() -> bytes:
    instance = celaut.Instance()
    slot = celaut.Instance.Uri_Slot()
    slot.internal_port = 5000
    slot.uri.append(celaut.Instance.Uri(ip="127.0.0.1", port=5000))
    instance.uri_slot.append(slot)
    return instance.SerializeToString()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CommandsInstancesCloudHypervisorTests(unittest.TestCase):
    def _build_db(self, db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE local_instances (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    ip TEXT,
                    father_id TEXT,
                    balance_mu TEXT,
                    mem_limit INTEGER,
                    serialized_instance BLOB,
                    service_id TEXT,
                    virtualizer TEXT DEFAULT NULL
                )
                """
            )
            cur.execute("CREATE TABLE clients (id TEXT PRIMARY KEY)")
            cur.execute(
                """
                CREATE TABLE delegated_instances (
                    token_delegation TEXT PRIMARY KEY,
                    id TEXT,
                    peer_id TEXT,
                    father_id TEXT,
                    serialized_instance BLOB,
                    service_id TEXT
                )
                """
            )
            cur.execute("INSERT INTO clients (id) VALUES (?)", ("client-1",))
            cur.execute(
                """
                INSERT INTO local_instances
                (id, name, ip, father_id, balance_mu, mem_limit, serialized_instance, service_id, virtualizer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "vm-ch",
                    "steady-falcon",
                    "192.168.200.10",
                    "client-1",
                    "1000",
                    128 * 1024 * 1024,
                    _serialized_instance(),
                    "svc-ch",
                    "ch",
                ),
            )
            cur.execute(
                """
                INSERT INTO local_instances
                (id, name, ip, father_id, balance_mu, mem_limit, serialized_instance, service_id, virtualizer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "vm-docker",
                    "brisk-orbit",
                    "172.17.0.2",
                    "client-1",
                    "900",
                    64 * 1024 * 1024,
                    _serialized_instance(),
                    "svc-docker",
                    "docker",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_instances_plain_shows_virtualizer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "instances.sqlite")
            self._build_db(db_path)
            with patch.object(instances_cmd, "DATABASE_FILE", db_path), patch.object(
                instances_cmd, "METADATA", tmpdir
            ), patch.object(
                instances_cmd,
                "_prune_stale_ch_instances",
                return_value=None,
            ), patch.object(
                instances_cmd,
                "get_vm_runtime_snapshot",
                return_value={
                    "pid": 1234,
                    "uptime_s": 42,
                    "mem_rss_bytes": 8 * 1024 * 1024,
                    "cgroup_memory_max_raw": str(128 * 1024 * 1024),
                    "cgroup_memory_max_bytes": 128 * 1024 * 1024,
                    "cgroup_memory_current_bytes": 12 * 1024 * 1024,
                },
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    instances_cmd.list_instances(groupable=False)

        rendered = out.getvalue()
        self.assertIn("Name: steady-falcon", rendered)
        self.assertIn("Virtualizer: ch", rendered)
        self.assertIn("Virtualizer: docker", rendered)
        self.assertNotIn("VM PID:", rendered)

    def test_instances_groupable_shows_runtime_only_for_ch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "instances.sqlite")
            self._build_db(db_path)

            def _snapshot(vmachine_id):
                if vmachine_id == "vm-ch":
                    return {
                        "pid": 1234,
                        "uptime_s": 65,
                        "mem_rss_bytes": 9 * 1024 * 1024,
                        "cgroup_memory_max_raw": str(50 * 1000 * 1000),
                        "cgroup_memory_max_bytes": 50 * 1000 * 1000,
                        "cgroup_memory_current_bytes": 11 * 1024 * 1024,
                    }
                return {
                    "pid": None,
                    "uptime_s": None,
                    "mem_rss_bytes": None,
                    "cgroup_memory_max_raw": None,
                    "cgroup_memory_max_bytes": None,
                    "cgroup_memory_current_bytes": None,
                }

            with patch.object(instances_cmd, "DATABASE_FILE", db_path), patch.object(
                instances_cmd, "METADATA", tmpdir
            ), patch.object(
                instances_cmd,
                "_prune_stale_ch_instances",
                return_value=None,
            ), patch.object(
                instances_cmd,
                "get_vm_runtime_snapshot",
                side_effect=_snapshot,
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    instances_cmd.list_instances(groupable=True)

        rendered = out.getvalue()
        self.assertIn("Virtualizer: ch", rendered)
        self.assertIn("VM PID: 1234", rendered)
        self.assertIn("VM Uptime: 1m 5s", rendered)
        self.assertIn("VM Memory (RSS): 9.00 MB", rendered)
        self.assertIn("VM Memory limit (cgroup): 47.68 MB", rendered)
        self.assertIn("VM Memory current (cgroup): 11.00 MB", rendered)
        self.assertIn("Virtualizer: docker", rendered)
        self.assertNotIn("VM PID: N/A", rendered)

    def test_list_instances_prunes_stale_ch_before_rendering(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "instances.sqlite")
            self._build_db(db_path)

            with patch.object(instances_cmd, "DATABASE_FILE", db_path), patch.object(
                instances_cmd,
                "_ch_instance_is_stale",
                side_effect=lambda instance_id: instance_id == "vm-ch",
            ), patch(
                "src.manager.manager.stop_instance",
                side_effect=Exception("cleanup failed"),
            ), patch(
                "src.virtualizers.ch.kill.kill",
                return_value=True,
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    instances_cmd.list_instances(groupable=False)

        rendered = out.getvalue()
        self.assertNotIn("ID: vm-ch", rendered)
        self.assertIn("ID: vm-docker", rendered)


if __name__ == "__main__":
    unittest.main()

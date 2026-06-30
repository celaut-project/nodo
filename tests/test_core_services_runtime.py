import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from src.core_services import runtime
    from protos import celaut_pb2 as celaut
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    runtime = None  # type: ignore[assignment]
    celaut = None  # type: ignore[assignment]


def _serialized_instance(ip="127.0.0.1", port=18080, internal_port=5000):
    instance = celaut.Instance()
    uri_slot = instance.uri_slot.add()
    uri_slot.internal_port = internal_port
    uri = uri_slot.uri.add()
    uri.ip = ip
    uri.port = port
    return instance.SerializeToString()


class _FakeCursor:
    def __init__(self, rows=None, raise_on_execute=None):
        self._rows = rows or []
        self._raise = raise_on_execute

    def execute(self, *args, **kwargs):
        if self._raise is not None:
            raise self._raise
        return self

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _patch_db(rows=None, raise_on_execute=None, raise_on_connect=None):
    """Patch sqlite3.connect + DATABASE_FILE config used by runtime.find_running_endpoint."""
    cursor = _FakeCursor(rows=rows, raise_on_execute=raise_on_execute)

    def fake_connect(_path):
        if raise_on_connect is not None:
            raise raise_on_connect
        return _FakeConn(cursor)

    return (
        patch.object(runtime._env_manager, "get", return_value="/tmp/fake-database.sqlite"),
        patch.object(runtime.sqlite3, "connect", side_effect=fake_connect),
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FindRunningEndpointTests(unittest.TestCase):
    def test_returns_endpoint_when_instance_running(self):
        rows = [(_serialized_instance(ip="10.0.0.5", port=9000),)]
        cfg, conn = _patch_db(rows=rows)
        with cfg, conn:
            self.assertEqual(
                runtime.find_running_endpoint("svc"), "http://10.0.0.5:9000"
            )

    def test_returns_none_when_no_rows(self):
        cfg, conn = _patch_db(rows=[])
        with cfg, conn:
            self.assertIsNone(runtime.find_running_endpoint("svc"))

    def test_missing_table_returns_none(self):
        # Simulate `no such table: local_instances`.
        cfg, conn = _patch_db(raise_on_execute=Exception("no such table: local_instances"))
        with cfg, conn:
            self.assertIsNone(runtime.find_running_endpoint("svc"))

    def test_unparseable_blob_returns_none(self):
        cfg, conn = _patch_db(rows=[(b"\xff\xff not a protobuf",)])
        with cfg, conn:
            self.assertIsNone(runtime.find_running_endpoint("svc"))

    def test_missing_database_file_returns_none(self):
        with patch.object(runtime._env_manager, "get", return_value=None):
            self.assertIsNone(runtime.find_running_endpoint("svc"))

    def test_connect_failure_returns_none(self):
        cfg, conn = _patch_db(raise_on_connect=sqlite3_error())
        with cfg, conn:
            self.assertIsNone(runtime.find_running_endpoint("svc"))


def sqlite3_error():
    import sqlite3

    return sqlite3.OperationalError("unable to open database file")


@contextmanager
def _stub_deps(acquire, launch):
    """Inject stub modules so runtime.py's lazy imports of source_application/execute
    resolve to our mocks without dragging in the real (bee_rpc-dependent) modules."""
    sa_mod = types.ModuleType("src.core_services.source_application")
    sa_mod.acquire_service = acquire
    exec_mod = types.ModuleType("src.commands.execute")
    exec_mod.execute = launch

    originals = {
        "src.core_services.source_application": sys.modules.get(
            "src.core_services.source_application"
        ),
        "src.commands.execute": sys.modules.get("src.commands.execute"),
    }
    sys.modules["src.core_services.source_application"] = sa_mod
    sys.modules["src.commands.execute"] = exec_mod
    try:
        yield
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EnsureCoreServiceRunningTests(unittest.TestCase):
    def test_running_instance_returns_without_acquire_or_launch(self):
        acquire = MagicMock(return_value=True)
        launch = MagicMock()
        with patch.object(
            runtime, "find_running_endpoint", return_value="http://127.0.0.1:18080"
        ) as find, _stub_deps(acquire, launch):
            result = runtime.ensure_core_service_running("svc")

        self.assertEqual(result, "http://127.0.0.1:18080")
        find.assert_called_once_with("svc")
        acquire.assert_not_called()
        launch.assert_not_called()

    def test_no_instance_acquire_false_launch_disabled_returns_none(self):
        acquire = MagicMock(return_value=False)
        launch = MagicMock()
        with patch.object(
            runtime, "find_running_endpoint", return_value=None
        ) as find, _stub_deps(acquire, launch):
            result = runtime.ensure_core_service_running("svc", launch=False)

        self.assertIsNone(result)
        acquire.assert_called_once_with("svc")
        launch.assert_not_called()
        # find_running_endpoint called at the top and again at the bottom.
        self.assertEqual(find.call_count, 2)

    def test_acquire_true_but_still_no_instance_returns_none(self):
        acquire = MagicMock(return_value=True)
        launch = MagicMock()
        with patch.object(
            runtime, "find_running_endpoint", return_value=None
        ), _stub_deps(acquire, launch):
            result = runtime.ensure_core_service_running("svc")

        self.assertIsNone(result)
        acquire.assert_called_once_with("svc")
        launch.assert_called_once_with("svc")

    def test_launch_error_is_swallowed_and_returns_none(self):
        acquire = MagicMock(return_value=False)
        launch = MagicMock(side_effect=RuntimeError("no gateway"))
        with patch.object(
            runtime, "find_running_endpoint", return_value=None
        ), _stub_deps(acquire, launch):
            # Must not raise even though execute() blows up.
            result = runtime.ensure_core_service_running("svc")

        self.assertIsNone(result)
        launch.assert_called_once_with("svc")

    def test_endpoint_appears_after_launch(self):
        # First check (top) returns None, second check (bottom) returns endpoint.
        acquire = MagicMock(return_value=True)
        launch = MagicMock()
        with patch.object(
            runtime,
            "find_running_endpoint",
            side_effect=[None, "http://10.0.0.9:7000"],
        ), _stub_deps(acquire, launch):
            result = runtime.ensure_core_service_running("svc")

        self.assertEqual(result, "http://10.0.0.9:7000")
        launch.assert_called_once_with("svc")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for `nodo pack` packer-endpoint resolution.

Covers `_resolve_packer_endpoint_by_id` (sqlite + protobuf lookup of a running
packer-service instance) and the `pack()` resolution order: service id first
(running instance), then the PACKER_SERVICE_URL override, then an actionable
failure message. No real packing or network I/O happens here.
"""
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.commands.packer.zip_with_dockerfile import pack as pack_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    pack_mod = None  # type: ignore[assignment]


def _serialized_instance(ip: str = "10.0.0.7", port: int = 8080) -> bytes:
    instance = celaut.Instance()
    slot = celaut.Instance.Uri_Slot()
    slot.internal_port = port
    slot.uri.append(celaut.Instance.Uri(ip=ip, port=port))
    instance.uri_slot.append(slot)
    return instance.SerializeToString()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolvePackerEndpointByIdTests(unittest.TestCase):
    def _build_db(self, db_path: str, service_id: str, serialized: bytes) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE local_instances (id TEXT PRIMARY KEY, service_id TEXT, serialized_instance BLOB)"
            )
            conn.execute(
                "INSERT INTO local_instances (id, service_id, serialized_instance) VALUES (?, ?, ?)",
                ("inst-1", service_id, serialized),
            )
            conn.commit()
        finally:
            conn.close()

    def test_id_resolves_to_running_instance_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "node.db")
            self._build_db(db_path, "packerid123", _serialized_instance("10.0.0.7", 8080))
            with patch.object(pack_mod.env_manager, "get", return_value=db_path):
                endpoint = pack_mod._resolve_packer_endpoint_by_id("packerid123")
            self.assertEqual(endpoint, "http://10.0.0.7:8080")

    def test_no_matching_row_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "node.db")
            self._build_db(db_path, "someotherid", _serialized_instance())
            with patch.object(pack_mod.env_manager, "get", return_value=db_path):
                endpoint = pack_mod._resolve_packer_endpoint_by_id("packerid123")
            self.assertIsNone(endpoint)

    def test_missing_table_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "node.db")
            sqlite3.connect(db_path).close()  # empty DB, no local_instances table
            with patch.object(pack_mod.env_manager, "get", return_value=db_path):
                endpoint = pack_mod._resolve_packer_endpoint_by_id("packerid123")
            self.assertIsNone(endpoint)

    def test_unparseable_blob_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "node.db")
            self._build_db(db_path, "packerid123", b"\xff\xfe not a protobuf")
            with patch.object(pack_mod.env_manager, "get", return_value=db_path):
                endpoint = pack_mod._resolve_packer_endpoint_by_id("packerid123")
            self.assertIsNone(endpoint)

    def test_empty_service_id_returns_none(self):
        self.assertIsNone(pack_mod._resolve_packer_endpoint_by_id(""))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PackResolutionOrderTests(unittest.TestCase):
    def test_id_set_and_instance_running_uses_instance_endpoint(self):
        with patch.object(pack_mod, "_resolve_packer_id", return_value="packerid123"), \
             patch.object(pack_mod, "_resolve_packer_endpoint_by_id", return_value="http://10.0.0.7:8080") as by_id, \
             patch.object(pack_mod, "_resolve_packer_url", return_value="http://override:8080") as by_url:
            endpoint = pack_mod._resolve_packer_endpoint()
        self.assertEqual(endpoint, "http://10.0.0.7:8080")
        by_id.assert_called_once_with("packerid123")
        by_url.assert_not_called()

    def test_id_set_no_instance_launches_packer_via_core_services(self):
        # No running instance, but the core-services runtime downloads+launches the
        # packer and yields a live endpoint — that endpoint is used and the URL
        # override is NOT consulted.
        with patch.object(pack_mod, "_resolve_packer_id", return_value="packerid123"), \
             patch.object(pack_mod, "_resolve_packer_endpoint_by_id", return_value=None), \
             patch.object(pack_mod, "_ensure_packer_running", return_value="http://10.0.0.9:8080") as ensure, \
             patch.object(pack_mod, "_resolve_packer_url", return_value="http://override:8080") as by_url:
            endpoint = pack_mod._resolve_packer_endpoint()
        self.assertEqual(endpoint, "http://10.0.0.9:8080")
        ensure.assert_called_once_with("packerid123")
        by_url.assert_not_called()

    def test_id_set_no_instance_and_launch_fails_falls_back_to_url_override(self):
        out = io.StringIO()
        with patch.object(pack_mod, "_resolve_packer_id", return_value="packerid123"), \
             patch.object(pack_mod, "_resolve_packer_endpoint_by_id", return_value=None), \
             patch.object(pack_mod, "_ensure_packer_running", return_value=None), \
             patch.object(pack_mod, "_resolve_packer_url", return_value="http://override:8080"):
            with redirect_stdout(out):
                endpoint = pack_mod._resolve_packer_endpoint()
        self.assertEqual(endpoint, "http://override:8080")
        self.assertIn("packerid123", out.getvalue())

    def test_no_id_uses_url_override(self):
        with patch.object(pack_mod, "_resolve_packer_id", return_value=None), \
             patch.object(pack_mod, "_resolve_packer_endpoint_by_id") as by_id, \
             patch.object(pack_mod, "_resolve_packer_url", return_value="http://override:8080"):
            endpoint = pack_mod._resolve_packer_endpoint()
        self.assertEqual(endpoint, "http://override:8080")
        by_id.assert_not_called()

    def test_id_resolves_from_core_services_list(self):
        # With no env var and no packer.PACKER_SERVICE_ID, the id is taken from a
        # {name: "packer", id: ...} entry in the unified core_services list.
        def fake_get(key, default=None):
            if key == "packer.PACKER_SERVICE_ID":
                return None
            if key == "core_services":
                return [{"name": "packer", "id": "coreid789"}]
            return default
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(pack_mod.env_manager, "get", side_effect=fake_get), \
             patch("src.core_services._env_manager.get", side_effect=fake_get):
            self.assertEqual(pack_mod._resolve_packer_id(), "coreid789")

    def test_neither_set_returns_none_and_pack_prints_guidance(self):
        out = io.StringIO()
        with patch.object(pack_mod, "_resolve_packer_id", return_value=None), \
             patch.object(pack_mod, "_resolve_packer_url", return_value=None):
            self.assertIsNone(pack_mod._resolve_packer_endpoint())
            # pack() should short-circuit with the no-packer message and never
            # touch prepare_directory / requests.
            with patch.object(pack_mod, "prepare_directory") as prep, \
                 patch.object(pack_mod, "requests") as req:
                with redirect_stdout(out):
                    result = pack_mod.pack("/some/project")
            prep.assert_not_called()
            req.post.assert_not_called()
        self.assertIsNone(result)
        guidance = out.getvalue()
        self.assertIn("PACKER_SERVICE_ID", guidance)
        self.assertIn("PACKER_SERVICE_URL", guidance)


class _Clock:
    """A deterministic stand-in for ``time.monotonic``.

    Returns each queued timestamp in order; once the queue is exhausted it keeps
    returning the last value, so a loop that keeps polling can never hang the test
    (the final value is chosen to be past the deadline).
    """

    def __init__(self, times):
        self._times = list(times)
        self._last = self._times[-1] if self._times else 0.0

    def __call__(self):
        if self._times:
            self._last = self._times.pop(0)
        return self._last


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WaitForPackerHealthTests(unittest.TestCase):
    """`_wait_for_packer_health` polls GET /health until 200 or the deadline.

    `time.sleep`/`time.monotonic` are patched so these run instantly, and
    `requests.get` is mocked so no real network I/O happens.
    """

    def _ok(self):
        resp = mock.Mock()
        resp.status_code = 200
        return resp

    def test_warm_path_returns_true_immediately_no_sleep(self):
        # An already-serving packer answers 200 on the first GET: True, zero sleeps,
        # and no "waiting..." noise printed (the cold-launch cost is not paid).
        out = io.StringIO()
        clock = _Clock([0.0])  # only the deadline base is read before the 200
        with patch.object(pack_mod.requests, "get", return_value=self._ok()) as get, \
             patch("time.sleep") as sleep, \
             patch("time.monotonic", new=clock):
            with redirect_stdout(out):
                healthy = pack_mod._wait_for_packer_health("http://packer:8080", timeout=300)
        self.assertTrue(healthy)
        get.assert_called_once_with("http://packer:8080/health", timeout=(5, 10))
        sleep.assert_not_called()
        self.assertEqual(out.getvalue(), "")

    def test_cold_path_retries_connection_errors_then_succeeds(self):
        # Packer VM still booting: a couple of connection failures, then 200 -> True.
        exc = pack_mod.requests.exceptions
        get_results = [
            exc.ConnectionError("connection refused"),
            exc.Timeout("read timed out"),
            self._ok(),
        ]
        # deadline base + one monotonic read per loop iteration (all under deadline).
        clock = _Clock([0.0, 1.0, 2.0, 3.0])
        with patch.object(pack_mod.requests, "get", side_effect=get_results) as get, \
             patch("time.sleep") as sleep, \
             patch("time.monotonic", new=clock):
            with redirect_stdout(io.StringIO()):
                healthy = pack_mod._wait_for_packer_health("http://packer:8080", timeout=300)
        self.assertTrue(healthy)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)  # slept between the two failed polls

    def test_never_healthy_returns_false_at_deadline(self):
        # Packer never serves: every GET fails; once monotonic passes the deadline
        # the loop gives up and returns False rather than blocking forever.
        exc = pack_mod.requests.exceptions
        # base=0, deadline=10; reads 3.0, 6.0 (retry), then 11.0 (>= deadline -> stop).
        clock = _Clock([0.0, 3.0, 6.0, 11.0])
        with patch.object(pack_mod.requests, "get",
                          side_effect=exc.ConnectionError("refused")) as get, \
             patch("time.sleep") as sleep, \
             patch("time.monotonic", new=clock):
            with redirect_stdout(io.StringIO()):
                healthy = pack_mod._wait_for_packer_health("http://packer:8080", timeout=10)
        self.assertFalse(healthy)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_non_200_response_is_retried_like_a_failure(self):
        # A serving-but-not-ready packer (e.g. 503) is retried, not treated as healthy.
        not_ready = mock.Mock()
        not_ready.status_code = 503
        clock = _Clock([0.0, 1.0, 2.0])
        with patch.object(pack_mod.requests, "get",
                          side_effect=[not_ready, self._ok()]) as get, \
             patch("time.sleep") as sleep, \
             patch("time.monotonic", new=clock):
            with redirect_stdout(io.StringIO()):
                healthy = pack_mod._wait_for_packer_health("http://packer:8080", timeout=300)
        self.assertTrue(healthy)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(sleep.call_count, 1)


if __name__ == "__main__":
    unittest.main()

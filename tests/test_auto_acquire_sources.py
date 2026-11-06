"""The automatic fallback to the sources of a service: `maintain.check_wanted_service` ->
`source_application.acquire_from_sources_if_enabled` -> `acquire_from_sources`.

A service that starts a dependency by hash reaches `check_wanted_service` through
`add_wanted`. Peers first; then, only with `service_sources.AUTO_ACQUIRE`, the sources that
the source-application lists, with a limit, and what is kept is only the id that was asked.
"""
import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from src.core_services import source_application as sa
    from src.core_services import source_download as sd
    from src.manager import maintain
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    sa = sd = maintain = None  # type: ignore[assignment]

WANTED = "ab" * 32
OTHER = "cd" * 32
MIB = 1024 * 1024


def _config(**values):
    """A `ConfigManager.get` that answers `values` (dotted keys) and the default otherwise."""
    return lambda key, default=None: values.get(key, default)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CheckWantedServiceTests(unittest.TestCase):
    def setUp(self):
        maintain.wanted_services.clear()
        maintain.wanted_services_retry.clear()
        maintain.wanted_from_sources.clear()
        maintain.wanted_from_sources.add(WANTED)
        self.addCleanup(maintain.wanted_from_sources.clear)

    def test_a_want_of_a_caller_that_the_node_does_not_know_asks_the_peers_only(self):
        # Audit N5.
        maintain.wanted_from_sources.clear()
        with patch.object(maintain, "fetch_service_from_peers", return_value=False) as peers, patch.object(
            sa, "acquire_in_background"
        ) as sources:
            maintain.check_wanted_service(WANTED)

        peers.assert_called_once_with(WANTED)
        sources.assert_not_called()
        self.assertEqual(maintain.wanted_services_retry, {WANTED})

    def test_a_peer_that_has_it_ends_it_and_the_sources_are_not_asked(self):
        with patch.object(maintain, "fetch_service_from_peers", return_value=True), patch.object(
            sa, "acquire_in_background"
        ) as sources:
            maintain.check_wanted_service(WANTED)

        sources.assert_not_called()
        self.assertEqual(maintain.wanted_services_retry, set())

    def test_the_sources_are_asked_on_their_thread_when_no_peer_has_it(self):
        with patch.object(maintain, "fetch_service_from_peers", return_value=False), patch.object(
            sa, "acquire_in_background", return_value=True
        ) as sources, patch.object(sa, "acquire_from_sources_if_enabled") as inline:
            maintain.check_wanted_service(WANTED)

        self.assertEqual(sources.call_args.args, (WANTED,))
        inline.assert_not_called()
        # In the queue: it is not retried until the thread says that the sources failed.
        self.assertEqual(maintain.wanted_services_retry, set())
        sources.call_args.kwargs["on_failure"](WANTED)
        self.assertEqual(maintain.wanted_services_retry, {WANTED})

    def test_a_service_that_nothing_gave_is_retried_later(self):
        with patch.object(maintain, "fetch_service_from_peers", return_value=False), patch.object(
            sa, "acquire_in_background", return_value=False
        ):
            maintain.check_wanted_service(WANTED)

        self.assertEqual(maintain.wanted_services_retry, {WANTED})

    def test_wait_asks_the_sources_in_this_thread(self):
        # `nodo get --now` runs in its own process and waits for the answer.
        for acquired, retry in ((True, set()), (False, {WANTED})):
            maintain.wanted_services_retry.clear()
            with patch.object(maintain, "fetch_service_from_peers", return_value=False), patch.object(
                sa, "acquire_from_sources_if_enabled", return_value=acquired
            ) as inline, patch.object(sa, "acquire_in_background") as background:
                maintain.check_wanted_service(WANTED, wait=True)

            inline.assert_called_once_with(WANTED)
            background.assert_not_called()
            self.assertEqual(maintain.wanted_services_retry, retry)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BackgroundTests(unittest.TestCase):
    """Audit F2 and N6: the manager thread never waits for a lookup or a download."""

    def setUp(self):
        sa._last_attempt.clear()
        maintain.wanted_services_retry.clear()
        maintain.wanted_from_sources.add(WANTED)
        self.addCleanup(maintain.wanted_from_sources.clear)
        for patcher in (
            patch.object(sa, "get_core_service_id", return_value="5a" * 32),
            patch.object(sa._env_manager, "get", side_effect=_config()),
            patch.object(maintain, "fetch_service_from_peers", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _wait_until(self, condition, seconds=10.0):
        end = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), end, "the thread of the automatic path did not end")
            time.sleep(0.01)

    def test_the_manager_does_not_wait_for_a_download_that_does_not_end(self):
        release = threading.Event()
        threads = []

        def slow_download(service_id, max_bytes, limits=None):
            threads.append(threading.current_thread().name)
            release.wait(30)
            return False

        with patch.object(sa, "acquire_from_sources", side_effect=slow_download):
            start = time.monotonic()
            maintain.check_wanted_service(WANTED)
            self.assertLess(time.monotonic() - start, 2)
            self._wait_until(lambda: threads)
            self.assertEqual(maintain.wanted_services_retry, set())
            release.set()
            self._wait_until(lambda: WANTED in maintain.wanted_services_retry)

        self.assertEqual(threads, ["source-acquire"])
        self.assertNotIn(WANTED, sa._queued)

    def test_the_lookup_and_the_start_of_the_source_application_are_not_on_the_manager_thread(self):
        # N6: the lookup makes the source-application available (it may start its VM).
        seen = []

        def ensure(core_id):
            seen.append((core_id, threading.current_thread().name))
            return None

        with patch.object(sa, "ensure_core_service_running", side_effect=ensure), patch.object(
            sa, "_validates_on_import", return_value=True
        ), redirect_stdout(io.StringIO()):
            maintain.check_wanted_service(WANTED)
            self._wait_until(lambda: WANTED in maintain.wanted_services_retry)

        self.assertEqual(seen, [("5a" * 32, "source-acquire")])

    def test_the_same_id_is_queued_once_and_the_queue_has_a_limit(self):
        release = threading.Event()
        with patch.object(sa, "acquire_from_sources", side_effect=lambda *a, **k: release.wait(30) and False), \
                patch.object(sa, "MAX_QUEUED", 2), redirect_stdout(io.StringIO()):
            self.assertTrue(sa.acquire_in_background(WANTED, on_failure=lambda sid: None))
            self.assertTrue(sa.acquire_in_background(WANTED, on_failure=lambda sid: None))
            self.assertTrue(sa.acquire_in_background(OTHER, on_failure=lambda sid: None))
            self.assertFalse(sa.acquire_in_background("ef" * 32, on_failure=lambda sid: None))
            release.set()
            self._wait_until(lambda: not sa._queued)

    def test_it_does_nothing_when_it_is_off(self):
        with patch.object(sa._env_manager, "get", side_effect=_config(**{"service_sources.AUTO_ACQUIRE": False})), \
                patch.object(sa, "acquire_from_sources") as acquire:
            self.assertFalse(sa.acquire_in_background(WANTED, on_failure=lambda sid: None))

        acquire.assert_not_called()

    def test_the_limits_of_time_and_pace_come_from_the_config(self):
        got = []
        with patch.object(sa._env_manager, "get", side_effect=_config(**{
            "service_sources.DEADLINE_SECONDS": 90, "service_sources.MIN_KIB_PER_SECOND": 4,
        })), patch.object(sa, "acquire_from_sources", side_effect=lambda sid, mb, limits=None: got.append(limits)):
            sa.acquire_from_sources_if_enabled(WANTED)

        self.assertEqual(got[0].deadline_s, 90)
        self.assertEqual(got[0].min_bytes_per_s, 4 * 1024)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AddWantedTests(unittest.TestCase):
    """Audit N5: who may make the node ask the sources, and what an id is."""

    def setUp(self):
        for collection in (maintain.wanted_services, maintain.wanted_services_retry, maintain.wanted_from_sources):
            collection.clear()
            self.addCleanup(collection.clear)

    def test_a_value_that_is_not_a_service_id_is_not_wanted(self):
        for value in ("", "ab", "ab" * 31, "ab" * 33, "zz" * 32, "ab" * 32 + "\n" + "cd" * 32, None):
            maintain.add_wanted(value, from_sources=True)
        self.assertEqual(maintain.wanted_services, set())
        self.assertEqual(maintain.wanted_from_sources, set())

    def test_by_default_a_want_is_for_the_peers_only(self):
        maintain.add_wanted(WANTED)
        self.assertEqual(maintain.wanted_services, {WANTED})
        self.assertEqual(maintain.wanted_from_sources, set())

    def test_a_known_caller_may_want_the_sources_too(self):
        maintain.add_wanted(WANTED)  # a stranger first: the peers only
        maintain.add_wanted(WANTED, from_sources=True)
        self.assertEqual(maintain.wanted_services, {WANTED})
        self.assertEqual(maintain.wanted_from_sources, {WANTED})

    def test_nodo_get_is_the_operator_and_may_want_the_sources(self):
        with tempfile.TemporaryDirectory() as inbox:
            open(os.path.join(inbox, WANTED), "w").close()
            with patch.object(maintain, "WANTED_INBOX_DIR", inbox + os.sep):
                maintain.drain_wanted_inbox()
        self.assertEqual(maintain.wanted_from_sources, {WANTED})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CallerMayWantTests(unittest.TestCase):
    def setUp(self):
        sa._wants_by_caller.clear()
        self.addCleanup(sa._wants_by_caller.clear)

    def _with_limit(self, limit):
        patcher = patch.object(sa._env_manager, "get", side_effect=_config(**{"service_sources.WANTS_PER_CALLER_PER_HOUR": limit}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_caller_has_its_own_limit_in_one_hour(self):
        self._with_limit(2)
        clock = [1000.0]
        with patch.object(sa.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(sa.caller_may_want("client-a"))
            self.assertTrue(sa.caller_may_want("client-a"))
            self.assertFalse(sa.caller_may_want("client-a"))
            self.assertTrue(sa.caller_may_want("client-b"))
            clock[0] += sa.WANTS_WINDOW_SECONDS
            self.assertTrue(sa.caller_may_want("client-a"))

    def test_zero_is_no_limit_and_a_negative_value_refuses(self):
        self._with_limit(0)
        self.assertTrue(all(sa.caller_may_want("client-a") for _ in range(50)))
        self._with_limit(-1)
        self.assertFalse(sa.caller_may_want("client-a"))

    def test_no_caller_is_refused(self):
        self._with_limit(10)
        self.assertFalse(sa.caller_may_want(""))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AcquireIfEnabledTests(unittest.TestCase):
    def setUp(self):
        sa._last_attempt.clear()
        # The node has a source-application unless a test says it has not.
        patcher = patch.object(sa, "get_core_service_id", return_value="aa" * 32)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _with_config(self, **values):
        patcher = patch.object(sa._env_manager, "get", side_effect=_config(**values))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_is_on_by_default_when_the_node_has_a_source_application(self):
        self._with_config()  # no service_sources keys at all
        with patch.object(sa, "get_core_service_id", return_value="aa" * 32), patch.object(
            sa, "acquire_from_sources", return_value=True
        ) as acquire:
            self.assertTrue(sa.acquire_from_sources_if_enabled(WANTED))

        self.assertEqual(acquire.call_args.args, (WANTED, 2048 * MIB))

    def test_without_a_source_application_it_does_nothing_and_says_nothing(self):
        self._with_config()
        out = io.StringIO()
        with patch.object(sa, "get_core_service_id", return_value=""), patch.object(
            sa, "acquire_from_sources"
        ) as acquire, redirect_stdout(out):
            self.assertFalse(sa.acquire_from_sources_if_enabled(WANTED))

        acquire.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(sa._last_attempt, {})

    def test_it_can_be_turned_off(self):
        self._with_config(**{"service_sources.AUTO_ACQUIRE": False})
        with patch.object(sa, "get_core_service_id", return_value="aa" * 32), patch.object(
            sa, "acquire_from_sources"
        ) as acquire:
            self.assertFalse(sa.acquire_from_sources_if_enabled(WANTED))

        acquire.assert_not_called()
        self.assertEqual(sa._last_attempt, {})

    def test_it_asks_the_sources_with_the_limit_in_bytes(self):
        self._with_config(**{"service_sources.AUTO_ACQUIRE": True, "service_sources.MAX_DOWNLOAD_MB": 3})
        with patch.object(sa, "acquire_from_sources", return_value=True) as acquire:
            self.assertTrue(sa.acquire_from_sources_if_enabled(WANTED))

        self.assertEqual(acquire.call_args.args, (WANTED, 3 * MIB))

    def test_a_limit_of_zero_is_no_limit_and_not_the_operator_path(self):
        self._with_config(**{"service_sources.AUTO_ACQUIRE": True, "service_sources.MAX_DOWNLOAD_MB": 0})
        with patch.object(sa, "acquire_from_sources", return_value=False) as acquire:
            sa.acquire_from_sources_if_enabled(WANTED)

        # 0 and not None: the download is still guarded.
        self.assertEqual(acquire.call_args.args, (WANTED, 0))

    def test_the_same_service_is_not_looked_up_again_before_the_cooldown(self):
        self._with_config(
            **{"service_sources.AUTO_ACQUIRE": True, "service_sources.RETRY_AFTER_SECONDS": 100}
        )
        clock = [1000.0]
        with patch.object(sa.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            sa, "acquire_from_sources", return_value=False
        ) as acquire:
            sa.acquire_from_sources_if_enabled(WANTED)
            clock[0] += 99
            sa.acquire_from_sources_if_enabled(WANTED)
            self.assertEqual(acquire.call_count, 1)
            clock[0] += 2
            sa.acquire_from_sources_if_enabled(WANTED)
            self.assertEqual(acquire.call_count, 2)
            # Another service has its own cooldown.
            sa.acquire_from_sources_if_enabled(OTHER)
            self.assertEqual(acquire.call_count, 3)

    def test_a_failure_never_reaches_the_manager(self):
        self._with_config(**{"service_sources.AUTO_ACQUIRE": True})
        out = io.StringIO()
        with patch.object(sa, "acquire_from_sources", side_effect=RuntimeError("boom")), redirect_stdout(out):
            self.assertFalse(sa.acquire_from_sources_if_enabled(WANTED))

        self.assertIn("boom", out.getvalue())

    def test_a_value_that_is_not_valid_does_not_ask(self):
        self._with_config(
            **{"service_sources.AUTO_ACQUIRE": True, "service_sources.MAX_DOWNLOAD_MB": -1}
        )
        with patch.object(sa, "acquire_from_sources") as acquire, redirect_stdout(io.StringIO()):
            self.assertFalse(sa.acquire_from_sources_if_enabled(WANTED))

        acquire.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AcquireFromSourcesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo-auto-acquire-")
        self.addCleanup(self._tmp.cleanup)
        self.registry = os.path.join(self._tmp.name, "registry")
        self.cache = os.path.join(self._tmp.name, "cache")
        os.makedirs(self.registry)
        from src.commands import import_bee as import_module

        self.import_module = import_module
        for patcher in (
            patch.object(import_module, "REGISTRY", self.registry),
            patch.object(import_module, "VALIDATE_ON_IMPORT", True),
            patch.object(sa._env_manager, "get", side_effect=_config(CACHE=self.cache + os.sep)),
            patch.object(sa, "get_core_service_id", return_value="sa-id"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _import_as(self, service_id):
        """An `import_bee` that puts `service_id` in the registry, as a real import does."""
        def fake(path):
            self.assertTrue(os.path.exists(path))
            open(os.path.join(self.registry, service_id), "w").close()
            return service_id

        return fake

    def _download_writing(self, data=b"bee"):
        def fake(url, destination, max_bytes, timeout_s=60, limits=None):
            with open(destination, "wb") as f:
                f.write(data)
            return len(data)

        return fake

    def test_it_needs_the_source_application(self):
        out = io.StringIO()
        with patch.object(sa, "get_core_service_id", return_value=None), redirect_stdout(out):
            self.assertFalse(sa.acquire_from_sources(WANTED, 10 * MIB))

        self.assertIn("no 'source-application' core service configured", out.getvalue())

    def test_a_limit_without_validation_on_import_does_nothing(self):
        out = io.StringIO()
        with patch.object(self.import_module, "VALIDATE_ON_IMPORT", False), patch.object(
            sa, "__lookup_sources"
        ) as lookup, redirect_stdout(out):
            self.assertFalse(sa.acquire_from_sources(WANTED, 10 * MIB))

        lookup.assert_not_called()
        self.assertIn("VALIDATE_ON_IMPORT", out.getvalue())

    def test_the_service_that_was_asked_for_is_kept(self):
        with patch.object(sa, "__lookup_sources", return_value=["https://h/m"]), patch.object(
            sa, "download_to_file", side_effect=self._download_writing()
        ) as download, patch.object(
            self.import_module, "import_bee", side_effect=self._import_as(WANTED)
        ), redirect_stdout(io.StringIO()):
            self.assertTrue(sa.acquire_from_sources(WANTED, 5 * MIB))

        self.assertEqual(download.call_args.args[0], "https://h/m")
        self.assertEqual(download.call_args.args[2], 5 * MIB)
        self.assertTrue(os.path.exists(os.path.join(self.registry, WANTED)))
        self.assertEqual(os.listdir(os.path.join(self.cache, "source_downloads")), [])

    def test_another_service_is_removed_and_the_next_source_is_tried(self):
        imports = [self._import_as(OTHER), self._import_as(WANTED)]
        removed = []
        with patch.object(sa, "__lookup_sources", return_value=["https://h/a", "https://h/b"]), patch.object(
            sa, "download_to_file", side_effect=self._download_writing()
        ), patch.object(
            self.import_module, "import_bee", side_effect=lambda path: imports.pop(0)(path)
        ), patch(
            "src.commands.remove.remove", side_effect=lambda sid: removed.append(sid)
        ), redirect_stdout(io.StringIO()):
            self.assertTrue(sa.acquire_from_sources(WANTED, 5 * MIB))

        self.assertEqual(removed, [OTHER])

    def test_a_service_that_was_already_there_is_not_removed(self):
        open(os.path.join(self.registry, OTHER), "w").close()  # held before the import
        removed = []
        with patch.object(sa, "__lookup_sources", return_value=["https://h/a"]), patch.object(
            sa, "download_to_file", side_effect=self._download_writing()
        ), patch.object(
            self.import_module, "import_bee", return_value=OTHER
        ), patch(
            "src.commands.remove.remove", side_effect=lambda sid: removed.append(sid)
        ), redirect_stdout(io.StringIO()):
            self.assertFalse(sa.acquire_from_sources(WANTED, 5 * MIB))

        self.assertEqual(removed, [])

    def test_a_download_that_is_refused_goes_on_to_the_next_source(self):
        def download(url, destination, max_bytes, timeout_s=60, limits=None):
            if url == "https://h/big":
                with open(destination, "wb") as f:
                    f.write(b"part")  # a part was written before the limit
                raise sd.SourceDownloadError("the source is larger than the limit")
            return self._download_writing()(url, destination, max_bytes)

        with patch.object(sa, "__lookup_sources", return_value=["https://h/big", "https://h/ok"]), patch.object(
            sa, "download_to_file", side_effect=download
        ), patch.object(
            self.import_module, "import_bee", side_effect=self._import_as(WANTED)
        ), redirect_stdout(io.StringIO()):
            self.assertTrue(sa.acquire_from_sources(WANTED, 5 * MIB))

        self.assertEqual(os.listdir(os.path.join(self.cache, "source_downloads")), [])

    def test_a_file_that_does_not_import_is_not_a_service(self):
        with patch.object(sa, "__lookup_sources", return_value=["https://h/a"]), patch.object(
            sa, "download_to_file", side_effect=self._download_writing(b"junk")
        ), patch.object(
            self.import_module, "import_bee", return_value=None
        ), redirect_stdout(io.StringIO()):
            self.assertFalse(sa.acquire_from_sources(WANTED, 5 * MIB))

        self.assertEqual(os.listdir(os.path.join(self.cache, "source_downloads")), [])

    def test_the_operators_path_is_not_changed(self):
        # No limit given: the sources go to `download_from_manifest_url` as before, with no
        # guard, no temporary file and no check of VALIDATE_ON_IMPORT.
        with patch.object(self.import_module, "VALIDATE_ON_IMPORT", False), patch.object(
            sa, "__lookup_sources", return_value=["https://h/m"]
        ), patch.object(
            sa, "download_from_manifest_url", return_value={"service_id": WANTED}
        ) as legacy, patch.object(sa, "download_to_file") as capped, redirect_stdout(io.StringIO()):
            self.assertTrue(sa.acquire_from_sources(WANTED))

        legacy.assert_called_once_with("https://h/m")
        capped.assert_not_called()

    def test_acquire_service_is_the_peers_then_the_operators_path(self):
        with patch("src.manager.maintain.fetch_service_from_peers", return_value=False), patch.object(
            sa, "__lookup_sources", return_value=["https://h/m"]
        ), patch.object(
            sa, "download_from_manifest_url", return_value={"service_id": WANTED}
        ) as legacy, redirect_stdout(io.StringIO()):
            self.assertTrue(sa.acquire_service(WANTED))

        legacy.assert_called_once_with("https://h/m")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LookupSourcesTests(unittest.TestCase):
    def setUp(self):
        self.lookup = getattr(sa, "__lookup_sources")

    def test_the_core_service_is_made_available_and_the_wanted_id_is_what_is_asked(self):
        with patch.object(sa, "ensure_core_service_running", return_value="http://10.0.0.2:8080") as ensure, patch.object(
            sa, "_fetch_bytes", return_value=b'["https://h/m"]'
        ) as fetch:
            urls = self.lookup(WANTED, "sa-id")

        ensure.assert_called_once_with("sa-id")
        self.assertEqual(fetch.call_args.args[0], f"http://10.0.0.2:8080/api/sources?hash={WANTED}")
        self.assertEqual(urls, ["https://h/m"])

    def test_the_source_application_is_not_asked_for_its_own_sources(self):
        def ensure(core_id):
            # Making the source-application available looks for it by the same route.
            self.assertEqual(self.lookup(core_id, "sa-id"), [])
            return None

        with patch.object(sa, "ensure_core_service_running", side_effect=ensure) as mocked, redirect_stdout(io.StringIO()):
            self.assertEqual(self.lookup(WANTED, "sa-id"), [])

        mocked.assert_called_once()


if __name__ == "__main__":
    unittest.main()

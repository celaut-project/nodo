"""The automatic fallback to the sources of a service: `maintain.check_wanted_service` ->
`source_application.acquire_from_sources_if_enabled` -> `acquire_from_sources`.

A service that starts a dependency by hash reaches `check_wanted_service` through
`add_wanted`. Peers first; then, only with `service_sources.AUTO_ACQUIRE`, the sources that
the source-application lists, with a limit, and what is kept is only the id that was asked.
"""
import io
import os
import tempfile
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

    def test_a_peer_that_has_it_ends_it_and_the_sources_are_not_asked(self):
        with patch.object(maintain, "fetch_service_from_peers", return_value=True), patch.object(
            sa, "acquire_from_sources_if_enabled"
        ) as sources:
            maintain.check_wanted_service(WANTED)

        sources.assert_not_called()
        self.assertEqual(maintain.wanted_services_retry, set())

    def test_the_sources_are_asked_when_no_peer_has_it(self):
        with patch.object(maintain, "fetch_service_from_peers", return_value=False), patch.object(
            sa, "acquire_from_sources_if_enabled", return_value=True
        ) as sources:
            maintain.check_wanted_service(WANTED)

        sources.assert_called_once_with(WANTED)
        self.assertEqual(maintain.wanted_services_retry, set())

    def test_a_service_that_nothing_gave_is_retried_later(self):
        with patch.object(maintain, "fetch_service_from_peers", return_value=False), patch.object(
            sa, "acquire_from_sources_if_enabled", return_value=False
        ):
            maintain.check_wanted_service(WANTED)

        self.assertEqual(maintain.wanted_services_retry, {WANTED})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AcquireIfEnabledTests(unittest.TestCase):
    def setUp(self):
        sa._last_attempt.clear()

    def _with_config(self, **values):
        patcher = patch.object(sa._env_manager, "get", side_effect=_config(**values))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_is_off_by_default_and_does_nothing(self):
        self._with_config()  # no service_sources keys at all
        with patch.object(sa, "acquire_from_sources") as acquire:
            self.assertFalse(sa.acquire_from_sources_if_enabled(WANTED))

        acquire.assert_not_called()
        self.assertEqual(sa._last_attempt, {})

    def test_it_asks_the_sources_with_the_limit_in_bytes(self):
        self._with_config(**{"service_sources.AUTO_ACQUIRE": True, "service_sources.MAX_DOWNLOAD_MB": 3})
        with patch.object(sa, "acquire_from_sources", return_value=True) as acquire:
            self.assertTrue(sa.acquire_from_sources_if_enabled(WANTED))

        acquire.assert_called_once_with(WANTED, 3 * MIB)

    def test_a_limit_of_zero_is_no_limit_and_not_the_operator_path(self):
        self._with_config(**{"service_sources.AUTO_ACQUIRE": True, "service_sources.MAX_DOWNLOAD_MB": 0})
        with patch.object(sa, "acquire_from_sources", return_value=False) as acquire:
            sa.acquire_from_sources_if_enabled(WANTED)

        # 0 and not None: the download is still guarded.
        acquire.assert_called_once_with(WANTED, 0)

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
        def fake(url, destination, max_bytes, timeout_s=60):
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
        def download(url, destination, max_bytes, timeout_s=60):
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

"""`maintain.drain_wanted_inbox` -- the channel between `nodo get` and the daemon.

`wanted_services` is in-memory and lives only inside whichever process runs the
manager thread; a `nodo get <id>` (no `--now`) invocation is a separate, short-lived
process and cannot reach it. It drops a marker file instead, and this is the one place
that reads it back in, feeding `add_wanted` exactly as if the daemon had wanted the
service on its own (as `abstract_input_service_iterable.py` does for a delegated
execution's missing dependency).
"""
import os
import tempfile
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from src.manager import maintain
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    maintain = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DrainWantedInboxTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo-wanted-inbox-")
        self.addCleanup(self._tmp.cleanup)
        self.inbox = os.path.join(self._tmp.name, "wanted") + os.sep
        self._patcher = patch.object(maintain, "WANTED_INBOX_DIR", self.inbox)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        maintain.wanted_services.clear()
        maintain.wanted_services_retry.clear()

    def _queue(self, *service_ids):
        os.makedirs(self.inbox, exist_ok=True)
        for service_id in service_ids:
            open(os.path.join(self.inbox, service_id), "a").close()

    def test_a_missing_inbox_is_not_an_error(self):
        maintain.drain_wanted_inbox()  # the directory is never created up front

    def test_every_queued_id_reaches_wanted_services(self):
        self._queue("aa" * 32, "bb" * 32)

        maintain.drain_wanted_inbox()

        self.assertEqual(maintain.wanted_services, {"aa" * 32, "bb" * 32})

    def test_a_drained_marker_file_is_removed(self):
        self._queue("aa" * 32)

        maintain.drain_wanted_inbox()

        self.assertEqual(os.listdir(self.inbox), [])

    def test_an_id_already_being_retried_is_not_queued_twice(self):
        # add_wanted's own dedup: a `get` for something the daemon already tried and
        # failed is folded into the existing retry, not raced with it.
        maintain.wanted_services_retry.add("aa" * 32)
        self._queue("aa" * 32)

        maintain.drain_wanted_inbox()

        self.assertEqual(maintain.wanted_services, set())
        self.assertEqual(maintain.wanted_services_retry, {"aa" * 32})


if __name__ == "__main__":
    unittest.main()

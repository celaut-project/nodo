"""One tick that raises must not end billing for the life of the process (issue #354).

Every function `manager_thread` calls promises never to raise, and each of those
promises is held up separately -- a convention, not a structure. The loop runs on a
daemon thread started in `src/serve.py` with nothing above it, so a single slip stops
billing, the cold sweeps, the activity window and the donation indexer, while the gRPC
server keeps answering and the node looks healthy from outside.

What is pinned here is the guard, not a supervisor: the pass is retried, the traceback
is logged, and a failure that persists backs off instead of hot-looping.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.manager import maintain
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    maintain = None  # type: ignore[assignment]


class _StopTheLoop(BaseException):
    """How a test gets out of a `while True` that is meant never to end.

    A `BaseException` rather than the usual `StopIteration`, and that is the point of
    the guard being tested: the loop catches `Exception`, so anything derived from it
    would be swallowed and the test would hang instead of failing.
    """


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ManagerLoopGuardTests(unittest.TestCase):

    def _loop(self, passes):
        """Run `_manager_loop` over ``passes``, ending it with `_StopTheLoop`."""
        said, slept = [], []
        with mock.patch.object(maintain, "_manager_pass", side_effect=passes) as pass_, \
                mock.patch.object(maintain.log, "LOGGER", said.append), \
                mock.patch.object(maintain, "sleep", slept.append):
            with self.assertRaises(_StopTheLoop):
                maintain._manager_loop()
        return pass_, said, slept

    def test_a_pass_that_raises_is_followed_by_another_one(self):
        pass_, _said, _slept = self._loop(
            [RuntimeError("a tick broke its promise"), _StopTheLoop]
        )
        self.assertEqual(pass_.call_count, 2)

    def test_the_traceback_is_logged_rather_than_the_exception_alone(self):
        # A tick that raised is a bug in that tick, and the line that says so is the
        # only thing an operator has to find it with.
        _pass, said, _slept = self._loop([RuntimeError("boom"), _StopTheLoop])
        reported = "\n".join(said)
        self.assertIn("[ERROR]", reported)
        self.assertIn("Traceback", reported)
        self.assertIn("boom", reported)

    def test_a_persistent_failure_backs_off_instead_of_hot_looping(self):
        _pass, _said, slept = self._loop(
            [RuntimeError("still broken"), RuntimeError("still broken"), _StopTheLoop]
        )
        self.assertEqual(slept, [maintain.MANAGER_FAILURE_BACKOFF] * 2)

    def test_a_pass_that_returns_normally_is_not_slept_over(self):
        # The pass ends in its own `sleep(MANAGER_ITERATION_TIME)`; the backoff is for
        # the failure, and adding it to a healthy tick would halve the tick rate.
        _pass, _said, slept = self._loop([1, _StopTheLoop])
        self.assertEqual(slept, [])

    def test_the_interval_count_survives_a_failure(self):
        # It is what schedules the long-interval work; reset on every failure, a node
        # failing intermittently would never reach a long interval at all.
        counts = []

        def record(count):
            counts.append(count)
            if len(counts) == 1:
                raise RuntimeError("boom")
            if len(counts) > 2:
                raise _StopTheLoop
            return count + 1

        with mock.patch.object(maintain, "_manager_pass", side_effect=record), \
                mock.patch.object(maintain.log, "LOGGER"), \
                mock.patch.object(maintain, "sleep"):
            with self.assertRaises(_StopTheLoop):
                maintain._manager_loop()
        self.assertEqual(counts, [0, 0, 1])

    def test_the_node_being_stopped_is_not_swallowed(self):
        # KeyboardInterrupt is not a tick failing; catching it would make this loop the
        # reason the node will not stop.
        with mock.patch.object(maintain, "_manager_pass",
                               side_effect=KeyboardInterrupt), \
                mock.patch.object(maintain.log, "LOGGER"), \
                mock.patch.object(maintain, "sleep"):
            with self.assertRaises(KeyboardInterrupt):
                maintain._manager_loop()


if __name__ == "__main__":
    unittest.main()

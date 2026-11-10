"""How closing time is shown by `nodo estimate`, `nodo execute` and `nodo instances` (#344, 5)."""
import calendar
import io
import sqlite3
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.commands import closing_display as display
    from src.commands import execute as execute_module
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

NOW = 1_800_000_000


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TextTests(unittest.TestCase):
    def test_time_left(self):
        self.assertEqual(display.time_left(0), "already passed")
        self.assertEqual(display.time_left(-5), "already passed")
        self.assertEqual(display.time_left(40), "in 40 s")
        self.assertEqual(display.time_left(180), "in 3 min")
        self.assertEqual(display.time_left(7800), "in 2 h 10 min")
        self.assertEqual(display.time_left(7200), "in 2 h")

    def test_zero_means_no_close_scheduled(self):
        self.assertEqual(display.describe_closes_at(0, NOW), "no close is scheduled")
        self.assertEqual(display.describe_closes_at(None, NOW), "no close is scheduled")

    def test_a_close_is_written_in_local_time_with_the_time_left(self):
        text = display.describe_closes_at(NOW + 7800, NOW)
        self.assertTrue(text.endswith("(in 2 h 10 min)"), text)
        self.assertRegex(text, r"^\d{4}-\d\d-\d\d \d\d:\d\d ")

    def test_the_lines_say_the_policy_and_that_it_is_not_a_promise(self):
        for name, fragment in (("refuse", "leaves the instance running"),
                               ("stop", "stops the instance"),
                               ("hibernate", "disk is kept")):
            text = "\n".join(display.closing_lines(name, NOW + 60, NOW))
            self.assertIn(f"At the close: {name}", text)
            self.assertIn(fragment, text)
            self.assertIn("not a promise", text)

    def test_an_unknown_policy_reads_as_refuse(self):
        self.assertIn("At the close: refuse", "\n".join(display.closing_lines("nonsense", 0, NOW)))

    def test_a_quote_gives_its_fields(self):
        quote = celaut_pb2.EstimatedCost(closes_at=NOW + 3600, on_close=celaut_pb2.ON_CLOSE_STOP)
        text = "\n".join(display.quote_closing_lines(quote, NOW))
        self.assertIn("At the close: stop", text)
        self.assertIn("in 1 h", text)
        old_node = "\n".join(display.quote_closing_lines(celaut_pb2.EstimatedCost(), NOW))
        self.assertIn("no close is scheduled", old_node)
        self.assertIn("At the close: refuse", old_node)

    def test_the_instance_cell(self):
        self.assertEqual(display.instance_closing_text(None, None), "-")
        self.assertEqual(display.instance_closing_text("stop", 0), "stop")
        self.assertTrue(display.instance_closing_text("hibernate", NOW + 60, NOW).startswith("hibernate, "))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StateAndCommitmentTests(unittest.TestCase):
    def test_a_hibernation_by_the_window_is_not_called_suspended(self):
        self.assertEqual(display.state_label(None, None), "running")
        self.assertEqual(display.state_label("2026-01-01 00:00:00", "hibernated_by_window"),
                         "hibernated (activity window)")
        self.assertEqual(display.state_label("x", "hibernated"), "hibernated")
        self.assertEqual(display.state_label("x", "hibernated_out_of_balance"),
                         "hibernated (out of balance)")
        self.assertEqual(display.state_label("x", "process_lost"), "suspended (process_lost)")
        self.assertEqual(display.state_label("x", None), "suspended")

    def _utc(self, ts):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))

    def test_closing_before_the_announced_time_is_a_broken_commitment(self):
        closes_at = NOW
        early = self._utc(NOW - 3600)
        self.assertTrue(display.broken_commitment(early, "hibernated_by_window", closes_at))

    def test_closing_at_or_just_before_the_time_is_not(self):
        closes_at = NOW
        for stopped in (NOW, NOW - 60, NOW + 10):
            self.assertFalse(display.broken_commitment(
                self._utc(stopped), "hibernated_by_window", closes_at), stopped)

    def test_other_states_and_no_close_are_never_a_broken_commitment(self):
        early = self._utc(NOW - 3600)
        self.assertFalse(display.broken_commitment(early, "hibernated", NOW))
        self.assertFalse(display.broken_commitment(early, "hibernated_by_window", 0))
        self.assertFalse(display.broken_commitment("not a date", "hibernated_by_window", NOW))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ExecuteTests(unittest.TestCase):
    def test_the_quote_is_printed_before_the_launch(self):
        quote = celaut_pb2.EstimatedCost(closes_at=NOW + 600, on_close=celaut_pb2.ON_CLOSE_HIBERNATE)
        out = io.StringIO()
        with mock.patch.object(display, "quote_before_launch", return_value=quote), \
                redirect_stdout(out):
            display.print_closing_before_launch("abc")
        self.assertIn("At the close: hibernate", out.getvalue())

    def test_no_quote_prints_nothing_and_never_raises(self):
        out = io.StringIO()
        with mock.patch.object(display, "quote_before_launch", return_value=None), \
                redirect_stdout(out):
            display.print_closing_before_launch("abc")
        self.assertEqual(out.getvalue(), "")

    def test_a_failing_quote_call_returns_none(self):
        with mock.patch("src.manager.manager.get_dev_clients", side_effect=RuntimeError("x")):
            self.assertIsNone(display.quote_before_launch("abc"))

    def _execute(self, **kwargs):
        with mock.patch.object(execute_module, "resolve_service_hash", return_value="abc"), \
                mock.patch.object(execute_module, "launch_via_gateway", return_value=None), \
                mock.patch.object(display, "print_closing_before_launch") as shown:
            execute_module.execute("abc", **kwargs)
        return shown

    def test_nodo_execute_asks_for_it_and_a_programmatic_caller_does_not(self):
        self.assertEqual(self._execute(show_closing=True).call_count, 1)
        self.assertEqual(self._execute().call_count, 0)
        self.assertEqual(self._execute(show_closing=True, silent=True).call_count, 0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class InstancesListingTests(unittest.TestCase):
    def setUp(self):
        from src.commands import instances
        self.instances = instances
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

    def test_the_closing_cell_shows_a_broken_commitment(self):
        cell = self.instances._closing_cell("hibernate", NOW, True)
        self.assertIn("closed before the close that was announced", cell)
        self.assertNotIn("closed before", self.instances._closing_cell("hibernate", NOW, False))
        self.assertEqual(self.instances._closing_cell(None, None, False), "-")


if __name__ == "__main__":
    unittest.main()

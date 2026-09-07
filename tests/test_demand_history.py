"""What this node was asked for, by hour of the day (issue #337).

The operator chooses the hours this machine works in, and the SCHEDULE page draws that
choice. This is what makes the choice answerable against something: which hours anybody
asks for, and -- the number the table exists for -- how much work is being turned away
because the window is shut.

The counters are written from three threads' worth of callers (the manager tick, the
launcher, the refusal paths) and folded into one row per local hour, so what is tested
here is the folding: that a peak stays a peak, that additive figures add, and that a
refusal behind a closed window is never confused with one for want of memory.
"""
import unittest
from datetime import datetime, timedelta

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.database.sql_connection import SQLConnection
    from src.utils import demand_history
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DemandHistoryTests(unittest.TestCase):
    def setUp(self):
        self.sc = SQLConnection()
        self._clear()
        self.addCleanup(self._clear)
        # A recorder of its own per test: the module-level one carries whatever the
        # rest of the suite happened to record.
        self.recorder = demand_history._Recorder()

    def _clear(self):
        self.sc._execute("DELETE FROM demand_history")

    def _rows(self):
        return {row["hour"]: row for row in self.sc.get_demand_history(days=2)}

    def test_an_hour_key_is_the_local_hour_and_sorts_as_time(self):
        key = demand_history.hour_key(datetime(2026, 9, 7, 14, 37))
        self.assertEqual(key, "2026-09-07T14")
        self.assertLess(demand_history.hour_key(datetime(2026, 9, 7, 9)), key)
        self.assertEqual(key[-2:], "14", "the last two characters are the hour of the clock")

    def test_instances_held_keeps_the_peak_and_the_rest_adds_up(self):
        # Two flushes of one hour: a restart mid-hour must not make a busy hour read as
        # quiet, and it must not double-count the launches either.
        hour = "2026-09-07T14"
        self.sc.add_demand_history(hour=hour, instances_held=6, mu_charged=1000, admissions=2)
        self.sc.add_demand_history(hour=hour, instances_held=2, mu_charged=500, refusals=1)

        row = self._rows()[hour]
        self.assertEqual(row["instances_held"], 6, "the peak was overwritten by a later dip")
        self.assertEqual(row["mu_charged"], 1500)
        self.assertEqual(row["admissions"], 2)
        self.assertEqual(row["refusals"], 1)

    def test_a_closed_window_refusal_is_counted_apart(self):
        self.recorder.record(refusals=1, refused_closed=1)
        self.recorder.record(refusals=1)
        self.recorder.flush()

        row = next(iter(self._rows().values()))
        self.assertEqual(row["refusals"], 2, "both are refusals")
        self.assertEqual(row["refused_closed"], 1, "only one was the operator's choice")

    def test_record_refusal_defaults_to_not_blaming_the_window(self):
        # `record_refusal()` with no argument is the capacity case, which must never
        # inflate the figure that reads as "the cost of my own hours".
        demand_history._RECORDER = self.recorder
        demand_history.record_refusal()
        self.recorder.flush()
        row = next(iter(self._rows().values()))
        self.assertEqual(row["refused_closed"], 0)

    def test_nothing_is_written_for_an_hour_with_no_events(self):
        # A quiet hour is an absent row, not a row of zeroes: the table should not grow
        # by 24 rows a day on an idle node.
        self.recorder.record()
        self.recorder.flush()
        self.assertEqual(self._rows(), {})

    def test_the_hour_is_flushed_when_it_turns_over(self):
        early = datetime(2026, 9, 7, 14, 5)
        later = early + timedelta(hours=1)
        self.recorder.record(admissions=1, moment=early)
        self.assertEqual(self._rows(), {}, "written before the hour was over")

        self.recorder.record(admissions=1, moment=later)
        rows = self._rows()
        self.assertIn("2026-09-07T14", rows, "the finished hour was not flushed")
        self.assertEqual(rows["2026-09-07T14"]["admissions"], 1)
        self.assertNotIn("2026-09-07T15", rows, "the current hour is still in memory")

    def test_reading_folds_a_period_into_the_hours_of_the_clock(self):
        # What the page draws: for each hour of the day, the worst hour of that name in
        # the period. A mean would flatten a machine that is busy every evening into one
        # that is mildly busy all day, which is the opposite of what choosing hours
        # needs to see.
        today = datetime.now()
        for day_offset, held in ((0, 3), (1, 9)):
            moment = today - timedelta(days=day_offset)
            self.sc.add_demand_history(
                hour=demand_history.hour_key(moment.replace(hour=22)),
                instances_held=held,
                refused_closed=day_offset + 1,
            )

        peaks = demand_history.by_hour_of_day(days=7)
        self.assertEqual(len(peaks), 24)
        self.assertEqual(peaks[22], 9, "kept the busier of the two 22:00s")
        self.assertEqual(peaks[3], 0, "an hour with no history reads as zero, not as absent")

        refused = demand_history.refused_by_hour_of_day(days=7)
        self.assertEqual(refused[22], 3, "refusals accumulate across days rather than peaking")

    def test_history_older_than_the_retention_is_pruned(self):
        old = demand_history.hour_key(datetime.now() - timedelta(days=200))
        recent = demand_history.hour_key(datetime.now() - timedelta(hours=2))
        self.sc.add_demand_history(hour=old, admissions=1)
        self.sc.add_demand_history(hour=recent, admissions=1)

        self.sc.prune_demand_history(keep_days=demand_history.RETENTION_DAYS)
        remaining = {row["hour"] for row in self.sc.get_demand_history(days=365)}
        self.assertNotIn(old, remaining)
        self.assertIn(recent, remaining)

    def test_a_write_that_fails_does_not_reach_the_caller(self):
        # This is a statistic about taking work. A node must not fail to take work
        # because it could not write one down.
        import unittest.mock

        with unittest.mock.patch.object(
            SQLConnection, "add_demand_history", side_effect=RuntimeError("disk full")
        ):
            self.recorder.record(admissions=1)
            self.recorder.flush()  # must not raise

    def test_reading_a_broken_table_reads_as_no_history(self):
        import unittest.mock

        with unittest.mock.patch.object(
            SQLConnection, "get_demand_history", side_effect=RuntimeError("no such table")
        ):
            self.assertEqual(demand_history.history(), [])
            self.assertEqual(demand_history.by_hour_of_day(), [0] * 24)


if __name__ == "__main__":
    unittest.main()

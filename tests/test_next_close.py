"""`activity_window.next_close`, `next_open` and `utc_spans` (issue #344, phase 2)."""
import os
import time
import unittest
from datetime import datetime
from unittest import mock

IMPORT_ERROR = None
try:
    from src.utils import activity_window
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    activity_window = None  # type: ignore[assignment]


def _at(hour, minute=0, day=4):
    return datetime(2026, 9, day, hour, minute)


def _ts(moment):
    return int(moment.timestamp())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NextCloseTests(unittest.TestCase):
    def _windows(self, *spans):
        values = {
            "activity_window.ENABLED": True,
            "activity_window.WINDOWS": [{"START": s, "END": e} for s, e in spans],
        }
        return mock.patch.object(
            activity_window.env_manager, "get",
            side_effect=lambda key, default=None: values.get(key, default),
        )

    def test_no_window_means_no_close(self):
        with mock.patch.object(activity_window.env_manager, "get",
                               side_effect=lambda key, default=None: default):
            self.assertEqual(activity_window.next_close(_at(12)), 0)
            self.assertEqual(activity_window.next_open(_at(12)), 0)

    def test_one_window_closes_at_its_end_today(self):
        with self._windows(("08:00", "18:00")):
            self.assertEqual(activity_window.next_close(_at(9)), _ts(_at(18)))

    def test_after_the_end_the_next_close_is_tomorrow(self):
        with self._windows(("08:00", "18:00")):
            self.assertEqual(activity_window.next_close(_at(19)), _ts(_at(18, day=5)))

    def test_a_window_that_wraps_midnight_closes_the_next_morning(self):
        with self._windows(("22:00", "06:00")):
            self.assertEqual(activity_window.next_close(_at(23)), _ts(_at(6, day=5)))
            self.assertEqual(activity_window.next_close(_at(3)), _ts(_at(6)))

    def test_with_two_windows_the_nearest_end_wins(self):
        with self._windows(("12:00", "13:00"), ("22:00", "06:00")):
            self.assertEqual(activity_window.next_close(_at(12, 30)), _ts(_at(13)))
            self.assertEqual(activity_window.next_close(_at(14)), _ts(_at(6, day=5)))

    def test_two_windows_that_touch_have_no_close_at_the_boundary(self):
        with self._windows(("08:00", "12:00"), ("12:00", "18:00")):
            self.assertEqual(activity_window.next_close(_at(9)), _ts(_at(18)))
            self.assertEqual(activity_window.next_open(_at(7)), _ts(_at(8)))
            # 12:00 is where one starts as the other ends: not an opening either.
            self.assertEqual(activity_window.next_open(_at(9)), _ts(_at(8, day=5)))

    def test_the_value_is_absolute_so_a_later_read_names_the_same_time(self):
        with self._windows(("08:00", "18:00")):
            self.assertEqual(activity_window.next_close(_at(9, 0)),
                             activity_window.next_close(_at(9, 1)))

    def test_next_open_when_closed(self):
        with self._windows(("08:00", "18:00")):
            self.assertEqual(activity_window.next_open(_at(19)), _ts(_at(8, day=5)))
            self.assertEqual(activity_window.next_open(_at(6)), _ts(_at(8)))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UtcSpansTests(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def _windows(self, *spans):
        values = {
            "activity_window.ENABLED": True,
            "activity_window.WINDOWS": [{"START": s, "END": e} for s, e in spans],
        }
        return mock.patch.object(
            activity_window.env_manager, "get",
            side_effect=lambda key, default=None: values.get(key, default),
        )

    def _zone(self, name):
        os.environ["TZ"] = name
        time.tzset()

    def test_a_local_window_is_converted_to_utc_minutes(self):
        self._zone("UTC")
        with self._windows(("08:00", "18:00")):
            self.assertEqual(activity_window.utc_spans(_at(12)), [(480, 1080)])

    def test_a_window_that_wraps_in_utc_keeps_end_before_start(self):
        self._zone("UTC")
        with self._windows(("22:00", "06:00")):
            self.assertEqual(activity_window.utc_spans(_at(12)), [(1320, 360)])

    def test_two_windows_give_two_spans(self):
        self._zone("UTC")
        with self._windows(("12:00", "13:00"), ("22:00", "06:00")):
            self.assertEqual(activity_window.utc_spans(_at(12)), [(720, 780), (1320, 360)])

    def test_no_window_gives_no_spans(self):
        with mock.patch.object(activity_window.env_manager, "get",
                               side_effect=lambda key, default=None: default):
            self.assertEqual(activity_window.utc_spans(_at(12)), [])

    def test_the_utc_span_moves_with_the_offset_of_the_day(self):
        # Madrid is UTC+1 in winter and UTC+2 in summer: 08:00 local is 07:00 or 06:00 UTC.
        self._zone("Europe/Madrid")
        with self._windows(("08:00", "18:00")):
            winter = activity_window.utc_spans(datetime(2026, 1, 15, 12))
            summer = activity_window.utc_spans(datetime(2026, 7, 15, 12))
        self.assertEqual(winter, [(420, 1020)])
        self.assertEqual(summer, [(360, 960)])

    def test_next_close_follows_a_clock_change(self):
        self._zone("Europe/Madrid")
        with self._windows(("08:00", "18:00")):
            # The night of 2026-03-29 the clock moves forward: 18:00 is 17:00 UTC on the
            # 28th and 16:00 UTC on the 29th, and the answer is the absolute instant.
            before = activity_window.next_close(datetime(2026, 3, 28, 19))
        self.assertEqual(datetime.utcfromtimestamp(before), datetime(2026, 3, 29, 16, 0))


if __name__ == "__main__":
    unittest.main()

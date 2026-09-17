"""`activity_window`: the hours this node accepts work in.

Three things are worth pinning here, because all three fail silently.

The first is the wrap around midnight. A node rented out overnight has a window whose
end is *before* its start, and read as a plain `start <= t < end` that window is the
empty set -- the node would refuse everything, all night, which is exactly when it was
supposed to be working. It is the case an operator renting out a personal PC actually
wants, so it is the case with the most assertions.

The second is what an absent or unusable schedule means. Every one of the ways of
saying "no window" -- switched off, an empty list, an entry whose start equals its end,
and a time that does not parse -- has to leave the node open. A malformed schedule that
closed the node instead would take it off the network over a typo, and say nothing that
pointed at the typo.

The third is that the schedule is a *list*: a night shift and a weekday lunch break are
two separate open stretches, and the node must be open in either one, not only the
first one configured.
"""
import unittest
from datetime import datetime
from unittest import mock

IMPORT_ERROR = None
try:
    from src.utils import activity_window
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    activity_window = None  # type: ignore[assignment]

MAINTAIN_IMPORT_ERROR = None
try:
    from src.manager import maintain as maintain_module
except Exception as import_exc:  # pragma: no cover - environment-dependent
    MAINTAIN_IMPORT_ERROR = import_exc
    maintain_module = None  # type: ignore[assignment]


def _at(hour, minute=0):
    return datetime(2026, 9, 4, hour, minute)


def _window(start, end):
    return {"START": start, "END": end}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ActivityWindowTests(unittest.TestCase):

    def _configured(self, *, enabled=None, windows=None, on_close=None):
        """The module reading a config made of these settings, keyed as it reads them."""
        values = {}
        if enabled is not None:
            values["activity_window.ENABLED"] = enabled
        if windows is not None:
            values["activity_window.WINDOWS"] = windows
        if on_close is not None:
            values["activity_window.ON_CLOSE"] = on_close
        return mock.patch.object(
            activity_window.env_manager,
            "get",
            side_effect=lambda key, default=None: values.get(key, default),
        )

    def setUp(self):
        # The malformed-window notice is announced once per process; the tests that
        # assert on it would otherwise depend on which ran first.
        activity_window._malformed_announced = False

    def test_a_node_with_no_window_configured_is_open(self):
        with self._configured():
            self.assertEqual(activity_window.windows(), [])
            self.assertTrue(activity_window.is_open(_at(3)))

    def test_the_hours_are_ignored_while_the_section_is_switched_off(self):
        with self._configured(enabled=False, windows=[_window("09:00", "17:00")]):
            self.assertEqual(activity_window.windows(), [])
            self.assertTrue(activity_window.is_open(_at(3)))

    def test_a_window_inside_one_day_is_open_from_its_start_to_its_end(self):
        with self._configured(enabled=True, windows=[_window("09:00", "17:00")]):
            self.assertFalse(activity_window.is_open(_at(8, 59)))
            # START is inclusive, END exclusive.
            self.assertTrue(activity_window.is_open(_at(9)))
            self.assertTrue(activity_window.is_open(_at(16, 59)))
            self.assertFalse(activity_window.is_open(_at(17)))
            self.assertFalse(activity_window.is_open(_at(23)))

    def test_a_window_whose_end_is_before_its_start_runs_through_midnight(self):
        with self._configured(enabled=True, windows=[_window("22:00", "06:00")]):
            self.assertFalse(activity_window.is_open(_at(21, 59)))
            self.assertTrue(activity_window.is_open(_at(22)))
            self.assertTrue(activity_window.is_open(_at(23, 59)))
            # The half an operator renting out a PC overnight cares about most.
            self.assertTrue(activity_window.is_open(_at(0)))
            self.assertTrue(activity_window.is_open(_at(5, 59)))
            self.assertFalse(activity_window.is_open(_at(6)))
            self.assertFalse(activity_window.is_open(_at(12)))

    def test_an_empty_list_is_always_open(self):
        with self._configured(enabled=True, windows=[]):
            self.assertEqual(activity_window.windows(), [])
            self.assertTrue(activity_window.is_open(_at(3)))

    def test_a_start_equal_to_its_end_is_dropped_rather_than_read_as_never_open(self):
        """A degenerate entry contributes nothing rather than opening the whole day.

        With one window that was the only other reading available, and the safe one:
        equal edges meant "always open" so enabling the section before choosing the
        hours refused nothing. With a list, a second entry can carry the real hours, so
        a degenerate one is simply empty -- it no longer needs to swallow the others.
        """
        with self._configured(enabled=True, windows=[_window("00:00", "00:00")]):
            self.assertEqual(activity_window.windows(), [])
            self.assertTrue(activity_window.is_open(_at(3)))
        with self._configured(
            enabled=True,
            windows=[_window("09:00", "09:00"), _window("18:00", "20:00")],
        ):
            self.assertFalse(activity_window.is_open(_at(3)))
            self.assertTrue(activity_window.is_open(_at(19)))

    def test_the_node_is_open_in_any_of_several_windows(self):
        # A night shift and a weekday lunch break: two unrelated open stretches.
        with self._configured(
            enabled=True,
            windows=[_window("22:00", "06:00"), _window("12:00", "13:00")],
        ):
            self.assertTrue(activity_window.is_open(_at(23)))
            self.assertTrue(activity_window.is_open(_at(12, 30)))
            self.assertFalse(activity_window.is_open(_at(9)))
            self.assertFalse(activity_window.is_open(_at(18)))

    def test_a_window_that_does_not_parse_leaves_the_whole_schedule_open_and_says_so(self):
        logged = []
        with self._configured(
            enabled=True,
            windows=[_window("9am", "17:00"), _window("22:00", "06:00")],
        ), mock.patch.object(activity_window, "_log", logged.append):
            self.assertEqual(activity_window.windows(), [])
            self.assertTrue(activity_window.is_open(_at(3)))
            # Once, not once per admission decision: this is read on every launch.
            activity_window.windows()
            activity_window.windows()
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("always open", logged[0])

    def test_only_stop_reaps_running_instances(self):
        with self._configured(
            enabled=True, windows=[_window("09:00", "17:00")], on_close="stop"
        ):
            self.assertTrue(activity_window.stops_running_instances())
        for value in ("refuse", "REFUSE", "", "something-else"):
            with self._configured(
                enabled=True, windows=[_window("09:00", "17:00")], on_close=value
            ):
                self.assertFalse(
                    activity_window.stops_running_instances(),
                    f"ON_CLOSE={value!r} must not destroy anything",
                )

    def test_stop_is_recognised_whatever_the_case(self):
        with self._configured(
            enabled=True, windows=[_window("09:00", "17:00")], on_close="STOP"
        ):
            self.assertTrue(activity_window.stops_running_instances())

    def test_the_refusal_names_every_window_it_is_refusing_outside_of(self):
        """The reason travels to whoever asked, so a closed node must not read broken."""
        with self._configured(
            enabled=True,
            windows=[_window("22:00", "06:00"), _window("12:00", "13:00")],
        ):
            reason = activity_window.closed_reason()
        self.assertIn("22:00", reason)
        self.assertIn("06:00", reason)
        self.assertIn("12:00", reason)
        self.assertIn("13:00", reason)
        self.assertIn("activity_window", reason)

    def test_the_clock_parser_takes_what_an_operator_types_and_nothing_else(self):
        self.assertEqual(activity_window.parse_clock("7:00").hour, 7)
        self.assertEqual(activity_window.parse_clock("07:05").minute, 5)
        self.assertEqual(activity_window.parse_clock("07:00:30").second, 30)
        for text in ("", "7", "24:00", "07:60", "9am", "07-00", "::", "abc:def"):
            self.assertIsNone(activity_window.parse_clock(text), text)


@unittest.skipIf(
    MAINTAIN_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {MAINTAIN_IMPORT_ERROR}",
)
class ClosingTimeReaperTests(unittest.TestCase):
    """`ON_CLOSE: stop` is the irreversible half, so it only ever runs when asked for.

    Every other combination has to leave running instances alone: destroying somebody's
    work mid-flight because a clock ticked is not something to arrive at by default.
    """

    def _run(self, *, is_open: bool, stops: bool, dev_ids=()):
        connection = mock.MagicMock()
        connection.get_all_internal_containers_ids.return_value = ["rented-1", "mine-1"]
        with mock.patch.object(maintain_module.activity_window, "is_open", return_value=is_open), \
                mock.patch.object(maintain_module.activity_window,
                                  "stops_running_instances", return_value=stops), \
                mock.patch.object(maintain_module, "sc", connection), \
                mock.patch.object(maintain_module, "descends_from_dev_client",
                                  side_effect=lambda id: id in dev_ids), \
                mock.patch.object(maintain_module, "stop_instance") as stop:
            maintain_module.enforce_activity_window()
        return stop

    def test_nothing_is_stopped_while_the_node_is_open(self):
        self._run(is_open=True, stops=True).assert_not_called()

    def test_nothing_is_stopped_when_on_close_only_refuses(self):
        self._run(is_open=False, stops=False).assert_not_called()

    def test_closing_time_stops_what_it_was_asked_to(self):
        stop = self._run(is_open=False, stops=True)
        self.assertEqual(
            sorted(call.kwargs["token"] for call in stop.call_args_list),
            ["mine-1", "rented-1"],
        )

    def test_the_operators_own_work_survives_closing_time(self):
        """The exemption `launch_service` applies at admission, applied at the reaper.

        The window is about renting this machine out after hours. An operator whose own
        `nodo execute` was killed at midnight would have been given a different feature
        from the one they switched on.
        """
        stop = self._run(is_open=False, stops=True, dev_ids=("mine-1",))
        stop.assert_called_once_with(token="rented-1")

    def test_an_instance_that_will_not_stop_does_not_take_the_tick_down(self):
        """This runs in the manager loop, which nothing above it catches."""
        connection = mock.MagicMock()
        connection.get_all_internal_containers_ids.return_value = ["a", "b"]
        with mock.patch.object(maintain_module.activity_window, "is_open", return_value=False), \
                mock.patch.object(maintain_module.activity_window,
                                  "stops_running_instances", return_value=True), \
                mock.patch.object(maintain_module, "sc", connection), \
                mock.patch.object(maintain_module, "descends_from_dev_client",
                                  return_value=False), \
                mock.patch.object(maintain_module, "stop_instance",
                                  side_effect=[Exception("busy"), None]) as stop:
            maintain_module.enforce_activity_window()
        # The second instance is still reached after the first one raised.
        self.assertEqual(stop.call_count, 2)


if __name__ == "__main__":
    unittest.main()

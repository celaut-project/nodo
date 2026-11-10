"""A peer whose stored schedule says it is closed is skipped (issue #344, phase 3)."""
import unittest
from datetime import datetime
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.utils.cost_functions import architecture_resources as ar
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _window(*spans):
    window = celaut.ActivityWindow()
    for start, end in spans:
        window.spans.add(start_minute=start, end_minute=end)
    return window


def _utc(hour, minute=0):
    return datetime(2026, 9, 4, hour, minute)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ScheduleClosedReasonTests(unittest.TestCase):
    def test_outside_every_span_it_is_closed(self):
        self.assertIsNotNone(ar.schedule_closed_reason(_window((480, 1080)), _utc(20)))

    def test_inside_a_span_it_is_open(self):
        self.assertIsNone(ar.schedule_closed_reason(_window((480, 1080)), _utc(9)))

    def test_the_end_is_not_in_the_span_and_the_start_is(self):
        self.assertIsNone(ar.schedule_closed_reason(_window((480, 1080)), _utc(8)))
        self.assertIsNotNone(ar.schedule_closed_reason(_window((480, 1080)), _utc(18)))

    def test_a_span_that_passes_midnight(self):
        night = _window((1320, 360))
        self.assertIsNone(ar.schedule_closed_reason(night, _utc(23)))
        self.assertIsNone(ar.schedule_closed_reason(night, _utc(3)))
        self.assertIsNotNone(ar.schedule_closed_reason(night, _utc(12)))

    def test_any_span_opens_it(self):
        two = _window((720, 780), (1320, 360))
        self.assertIsNone(ar.schedule_closed_reason(two, _utc(12, 30)))
        self.assertIsNotNone(ar.schedule_closed_reason(two, _utc(14)))

    def test_no_schedule_empty_or_unreadable_is_never_a_reason(self):
        self.assertIsNone(ar.schedule_closed_reason(None, _utc(3)))
        self.assertIsNone(ar.schedule_closed_reason(_window(), _utc(3)))
        self.assertIsNone(ar.schedule_closed_reason(_window((10, 10)), _utc(3)))
        self.assertIsNone(ar.schedule_closed_reason(_window((0, 5000)), _utc(3)))
        self.assertIsNone(ar.schedule_closed_reason(_window((-1, 100)), _utc(12)))
        # One span that cannot be read voids the whole schedule, as in the node itself.
        self.assertIsNone(ar.schedule_closed_reason(_window((480, 600), (7, 7)), _utc(20)))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ShouldSkipPeerTests(unittest.TestCase):
    def _skip(self, window, now=None):
        with mock.patch.object(ar, "stored_announcement", return_value=[]), \
                mock.patch.object(ar, "stored_activity_window", return_value=window), \
                mock.patch.object(ar, "datetime") as clock:
            clock.utcnow.return_value = now or _utc(20)
            return ar.should_skip_peer("peer-a", "linux/amd64", celaut.Sysresources(), "x")

    def test_a_peer_that_the_schedule_says_is_closed_is_skipped(self):
        self.assertTrue(self._skip(_window((480, 1080))))

    def test_a_peer_that_the_schedule_says_is_open_is_asked(self):
        self.assertFalse(self._skip(_window((480, 1080)), _utc(9)))

    def test_a_peer_with_no_schedule_is_asked(self):
        self.assertFalse(self._skip(None))

    def test_the_stored_window_is_read_from_the_announcement(self):
        peer = celaut.Peer()
        peer.activity_window.spans.add(start_minute=480, end_minute=1080)
        peer.activity_window.on_close = celaut.ON_CLOSE_STOP
        sc = mock.MagicMock()
        sc.get_peer_advertisement.return_value = peer.SerializeToString()
        with mock.patch("src.database.sql_connection.SQLConnection", return_value=sc):
            window = ar.stored_activity_window("peer-a")
            self.assertEqual([(s.start_minute, s.end_minute) for s in window.spans], [(480, 1080)])
            sc.get_peer_advertisement.return_value = celaut.Peer().SerializeToString()
            self.assertIsNone(ar.stored_activity_window("peer-a"))
            sc.get_peer_advertisement.return_value = b"\xff\xff not a peer"
            self.assertIsNone(ar.stored_activity_window("peer-a"))


if __name__ == "__main__":
    unittest.main()

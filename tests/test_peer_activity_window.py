"""The announcement carries the schedule in UTC and `on_close` (issue #344)."""
import os
import time
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.gateway import utils as gateway_utils
    from src.identity.node_identity import canonical_peer_content_digest
    from src.utils import activity_window
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerActivityWindowTests(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._restore)

    def _restore(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def _peer(self, **window):
        values = {f"activity_window.{key}": value for key, value in window.items()}
        with mock.patch.object(
                activity_window.env_manager, "get",
                side_effect=lambda key, default=None: values.get(key, default)), \
                mock.patch.object(gateway_utils, "_local_payment_contracts", return_value=[]), \
                mock.patch("src.reputation_system.fetch.local_proofs", return_value=[]), \
                mock.patch("src.utils.cost_functions.architecture_resources.announced_resources",
                           return_value=[]), \
                mock.patch("src.utils.cost_functions.general_cost_functions.node_advertised_rates",
                           return_value={}):
            return gateway_utils._unsigned_peer([], quiet=True)

    def _spans(self, peer):
        return [(s.start_minute, s.end_minute) for s in peer.activity_window.spans]

    def test_the_spans_are_in_utc_and_carry_on_close(self):
        peer = self._peer(ENABLED=True, WINDOWS=[{"START": "08:00", "END": "18:00"}],
                          ON_CLOSE="hibernate")
        self.assertEqual(self._spans(peer), [(480, 1080)])
        self.assertEqual(peer.activity_window.on_close, celaut_pb2.ON_CLOSE_HIBERNATE)

    def test_a_window_that_passes_midnight_keeps_end_before_start(self):
        peer = self._peer(ENABLED=True, WINDOWS=[{"START": "22:00", "END": "06:00"}])
        self.assertEqual(self._spans(peer), [(1320, 360)])

    def test_two_windows_give_two_spans(self):
        peer = self._peer(ENABLED=True, WINDOWS=[{"START": "12:00", "END": "13:00"},
                                                 {"START": "22:00", "END": "06:00"}])
        self.assertEqual(self._spans(peer), [(720, 780), (1320, 360)])

    def test_no_window_gives_an_empty_message_that_is_present(self):
        peer = self._peer(ENABLED=False, ON_CLOSE="stop")
        self.assertTrue(peer.HasField("activity_window"))
        self.assertEqual(self._spans(peer), [])
        # No window: nothing closes, so the node promises nothing.
        self.assertEqual(peer.activity_window.on_close, celaut_pb2.ON_CLOSE_REFUSE)

    def test_a_changed_schedule_gives_a_new_digest(self):
        one = self._peer(ENABLED=True, WINDOWS=[{"START": "08:00", "END": "18:00"}])
        other = self._peer(ENABLED=True, WINDOWS=[{"START": "08:00", "END": "19:00"}])
        self.assertNotEqual(canonical_peer_content_digest(one),
                            canonical_peer_content_digest(other))

    def test_an_absent_schedule_and_an_empty_one_are_different_claims(self):
        empty = self._peer(ENABLED=False)
        absent = celaut_pb2.Peer()
        absent.CopyFrom(empty)
        absent.ClearField("activity_window")
        self.assertNotEqual(canonical_peer_content_digest(empty),
                            canonical_peer_content_digest(absent))

    def test_a_change_of_utc_offset_changes_the_digest(self):
        os.environ["TZ"] = "Europe/Madrid"
        time.tzset()
        window = dict(ENABLED=True, WINDOWS=[{"START": "08:00", "END": "18:00"}])
        with mock.patch.object(activity_window, "datetime") as clock:
            from datetime import datetime as real
            clock.now.return_value = real(2026, 1, 15, 12)
            clock.combine = real.combine
            clock.utcfromtimestamp = real.utcfromtimestamp
            winter = self._peer(**window)
            clock.now.return_value = real(2026, 7, 15, 12)
            summer = self._peer(**window)
        self.assertNotEqual(canonical_peer_content_digest(winter),
                            canonical_peer_content_digest(summer))


if __name__ == "__main__":
    unittest.main()

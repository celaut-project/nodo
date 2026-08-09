"""is_open() caches its result for a short TTL instead of reconnecting on every
call (issue #236: accumulating several addresses per peer means the ~15 call sites
that use it would otherwise pay its 1s-timeout connect much more often)."""
import unittest
from unittest import mock

from src.utils import utils


class IsOpenCacheTests(unittest.TestCase):
    def setUp(self):
        utils._is_open_cache.clear()

    def test_second_call_within_ttl_does_not_reconnect(self):
        with mock.patch.object(utils.socket, "socket") as socket_cls:
            sock = socket_cls.return_value
            self.assertTrue(utils.is_open(ip="1.2.3.4", port=80))
            self.assertTrue(utils.is_open(ip="1.2.3.4", port=80))
            self.assertEqual(sock.connect.call_count, 1)

    def test_different_port_is_not_cached_together(self):
        with mock.patch.object(utils.socket, "socket") as socket_cls:
            sock = socket_cls.return_value
            utils.is_open(ip="1.2.3.4", port=80)
            utils.is_open(ip="1.2.3.4", port=81)
            self.assertEqual(sock.connect.call_count, 2)

    def test_expired_entry_reconnects(self):
        with mock.patch.object(utils.socket, "socket") as socket_cls, \
             mock.patch.object(utils, "time") as time_mod:
            sock = socket_cls.return_value
            time_mod.monotonic.side_effect = [0.0, 1000.0]
            utils.is_open(ip="1.2.3.4", port=80)
            utils.is_open(ip="1.2.3.4", port=80)
            self.assertEqual(sock.connect.call_count, 2)

    def test_failed_connection_is_also_cached(self):
        with mock.patch.object(utils.socket, "socket") as socket_cls:
            socket_cls.return_value.connect.side_effect = OSError("refused")
            self.assertFalse(utils.is_open(ip="1.2.3.4", port=80))
            self.assertFalse(utils.is_open(ip="1.2.3.4", port=80))
            self.assertEqual(socket_cls.return_value.connect.call_count, 1)


if __name__ == "__main__":
    unittest.main()

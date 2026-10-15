"""Re-announcing this node to known peers when its announcement changes.

``announcement_change_tick`` (src/manager/announcement_change.py) is the manager-loop
hook that compares the digest of what ``generate_full_node_peer_info`` would announce
against the one last pushed, and on a difference sends the new ``Peer`` to every
already-known peer with ``IntroducePeer``. The digest is kept on disk, so a change made
by a configuration edit -- which restarts the node -- is still seen as one.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    import src.manager.announcement_change as announcement_change
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    announcement_change = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AnnouncementChangeTickTests(unittest.TestCase):
    def setUp(self):
        # Module-level state, reset between tests so one test's interval gate does
        # not leak into the next.
        announcement_change._last_check_monotonic = None
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        path = os.path.join(self.cache.name, announcement_change.DIGEST_FILE)
        patcher = patch.object(announcement_change, "_digest_path", return_value=path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tick(self, digest, enabled=True, interval=0.0):
        with patch.object(announcement_change, "_is_enabled", return_value=enabled), \
             patch.object(announcement_change, "_check_interval_seconds", return_value=interval), \
             patch.object(announcement_change.gateway_utils, "announcement_digest", return_value=digest), \
             patch.object(announcement_change, "_announce_to_known_peers") as announce:
            announcement_change.announcement_change_tick()
        return announce

    def test_nothing_recorded_counts_as_a_change(self):
        # A fresh cache, or the first boot of a node from before this hook: peers may
        # hold any older announcement, so they are told once.
        announce = self._tick("aaa")
        announce.assert_called_once()
        self.assertEqual(announcement_change._last_announced_digest(), "aaa")

    def test_unchanged_announcement_does_not_announce(self):
        self._tick("aaa")
        announce = self._tick("aaa")
        announce.assert_not_called()

    def test_changed_announcement_announces_and_records_it(self):
        self._tick("aaa")
        announce = self._tick("bbb")
        announce.assert_called_once()
        self.assertEqual(announcement_change._last_announced_digest(), "bbb")

    def test_the_record_survives_a_restart(self):
        # The TUI restarts the node on every config edit; module state does not
        # survive that, the file does.
        self._tick("aaa")
        announcement_change._last_check_monotonic = None
        self._tick("aaa").assert_not_called()
        announcement_change._last_check_monotonic = None
        self._tick("bbb").assert_called_once()

    def test_disabled_does_nothing(self):
        announce = self._tick("aaa", enabled=False)
        announce.assert_not_called()
        self.assertIsNone(announcement_change._last_announced_digest())

    def test_self_gates_to_its_own_interval(self):
        self._tick("aaa", interval=60.0)
        announce = self._tick("bbb", interval=60.0)
        announce.assert_not_called()

    def test_a_failed_round_is_not_recorded(self):
        with patch.object(announcement_change, "_is_enabled", return_value=True), \
             patch.object(announcement_change, "_check_interval_seconds", return_value=0.0), \
             patch.object(announcement_change.gateway_utils, "announcement_digest", return_value="aaa"), \
             patch.object(announcement_change, "_announce_to_known_peers", side_effect=RuntimeError("db down")):
            announcement_change.announcement_change_tick()  # must not raise
        self.assertIsNone(announcement_change._last_announced_digest())

    def test_tick_never_raises(self):
        with patch.object(announcement_change, "_is_enabled", side_effect=RuntimeError("boom")):
            announcement_change.announcement_change_tick()  # must not raise

    def test_the_former_setting_names_are_still_read(self):
        values = {"network.REANNOUNCE_ON_NETWORK_CHANGE": False}
        with patch.object(
            announcement_change.env_manager, "get",
            side_effect=lambda key, default=None: values.get(key, default),
        ):
            self.assertFalse(announcement_change._is_enabled())
        values = {"network.REANNOUNCE_ON_CHANGE": True, "network.REANNOUNCE_ON_NETWORK_CHANGE": False}
        with patch.object(
            announcement_change.env_manager, "get",
            side_effect=lambda key, default=None: values.get(key, default),
        ):
            self.assertTrue(announcement_change._is_enabled())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AnnounceToKnownPeersTests(unittest.TestCase):
    def test_announces_to_every_known_peer(self):
        result = MagicMock()
        result.token = "peer-id"
        with patch.object(announcement_change.sc, "get_peers_id", return_value=["peer-a", "peer-b"]), \
             patch.object(announcement_change, "generate_full_node_peer_info", return_value=MagicMock()) as generate, \
             patch.object(announcement_change, "get_client_id_on_other_peer", return_value="client-1"), \
             patch.object(
                 announcement_change, "peer_channel",
                 side_effect=lambda peer_id: MagicMock(),
             ), \
             patch.object(announcement_change.BeeClient, "introduce_peer", return_value=result) as introduce:
            announcement_change._announce_to_known_peers()
        self.assertEqual(introduce.call_count, 2)
        generate.assert_called_once_with(fresh=True)
        for call in introduce.call_args_list:
            self.assertEqual(call.kwargs["client_id"], "client-1")

    def test_skips_a_peer_without_a_client_id(self):
        with patch.object(announcement_change.sc, "get_peers_id", return_value=["peer-a"]), \
             patch.object(announcement_change, "generate_full_node_peer_info", return_value=MagicMock()), \
             patch.object(announcement_change, "get_client_id_on_other_peer", side_effect=Exception("not available")), \
             patch.object(announcement_change, "peer_channel") as peer_channel, \
             patch.object(announcement_change.BeeClient, "introduce_peer") as introduce:
            announcement_change._announce_to_known_peers()
        peer_channel.assert_not_called()
        introduce.assert_not_called()

    def test_no_known_peers_builds_nothing(self):
        with patch.object(announcement_change.sc, "get_peers_id", return_value=[]), \
             patch.object(announcement_change, "generate_full_node_peer_info") as generate:
            announcement_change._announce_to_known_peers()
        generate.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AnnouncementDigestTests(unittest.TestCase):
    """``gateway.utils.announcement_digest``: what the tick compares, over the real build."""

    def _digest(self, executes_locally=True):
        from protos import celaut_pb2 as celaut
        from src.identity import node_identity
        from src.reputation_system import fetch as reputation_fetch
        from src.utils import benchmark, host_limits
        from src.utils.cost_functions import architecture_resources as ar
        from src.utils.cost_functions import general_cost_functions

        gateway_utils = announcement_change.gateway_utils
        with patch.object(gateway_utils, "_uris_for_all_interfaces",
                          return_value=[celaut.Instance.Uri(ip="81.2.3.4", port=40001)]), \
             patch.object(gateway_utils, "_local_payment_contracts", return_value=[]), \
             patch.object(gateway_utils, "share_prose_on_get_peer_info", return_value=False), \
             patch.object(general_cost_functions, "node_advertised_rates", return_value={"cpu": 10}), \
             patch.object(reputation_fetch, "local_proofs", return_value=[]), \
             patch.object(ar, "executes_locally", return_value=executes_locally), \
             patch.object(host_limits, "host_totals", return_value=(8, 16 << 30, 500 << 30)), \
             patch.object(host_limits, "ceilings", return_value=None), \
             patch.object(benchmark, "node_scores", return_value={}), \
             patch.object(node_identity, "sign_peer_payload") as sign:
            digest = gateway_utils.announcement_digest()
        sign.assert_not_called()
        return digest

    def test_the_same_announcement_has_the_same_digest(self):
        self.assertEqual(self._digest(), self._digest())

    def test_delegating_only_changes_the_announcement(self):
        # What closes the loop from the TUI's `run work here` lever to the peers: the
        # resources leave the announcement, so its digest moves and the tick sends it.
        self.assertNotEqual(self._digest(executes_locally=True), self._digest(executes_locally=False))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FreshAnnouncementTests(unittest.TestCase):
    def test_a_fresh_announcement_is_never_served_from_the_signature_cache(self):
        # Content that changed back (A -> B -> A) would otherwise come out of the cache
        # with A's old ts, and every peer holding B would drop it as stale.
        gateway_utils = announcement_change.gateway_utils
        gateway_utils._signed_peers["stale"] = (b"", 0.0)
        self.addCleanup(gateway_utils._signed_peers.clear)
        with patch.object(gateway_utils, "_uris_for_all_interfaces", return_value=[]), \
             patch.object(gateway_utils, "_build_peer", return_value=MagicMock()) as build:
            gateway_utils.generate_full_node_peer_info(fresh=True)
        build.assert_called_once()
        self.assertNotIn("stale", gateway_utils._signed_peers)


if __name__ == "__main__":
    unittest.main()

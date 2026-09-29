"""Re-announcing this node's address to known peers when it changes.

``network_change_tick`` (src/manager/network_change.py) is the manager-loop hook that
detects a change in the address ``_uris_for_all_interfaces`` would announce and pushes
it to every already-known peer with ``IntroducePeer``, so a node that moves networks
(LAN to mobile, a renewed dynamic public IP) does not have to wait for a peer's own
refresh to notice and, in the meantime, get penalised as unreachable.
"""
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    import src.manager.network_change as network_change
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    network_change = None  # type: ignore[assignment]


def _uri(ip: str, port: int) -> MagicMock:
    uri = MagicMock()
    uri.ip = ip
    uri.port = port
    return uri


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NetworkChangeTickTests(unittest.TestCase):
    def setUp(self):
        # Module-level state, reset between tests so one test's "already primed"
        # does not leak into the next.
        network_change._last_check_monotonic = None
        network_change._last_known_addresses = None

    def _tick(self, addresses, enabled=True, interval=0.0):
        with patch.object(network_change, "_is_enabled", return_value=enabled), \
             patch.object(network_change, "_check_interval_seconds", return_value=interval), \
             patch.object(
                 network_change.gateway_utils,
                 "_uris_for_all_interfaces",
                 return_value=[_uri(ip, port) for ip, port in addresses],
             ), \
             patch.object(network_change, "_announce_to_known_peers") as announce:
            network_change.network_change_tick()
        return announce

    def test_first_observation_primes_without_announcing(self):
        announce = self._tick([("81.2.3.4", 40001)])
        announce.assert_not_called()
        self.assertEqual(network_change._last_known_addresses, frozenset({("81.2.3.4", 40001)}))

    def test_unchanged_address_does_not_announce(self):
        self._tick([("81.2.3.4", 40001)])
        announce = self._tick([("81.2.3.4", 40001)])
        announce.assert_not_called()

    def test_changed_address_announces_and_updates_the_cache(self):
        self._tick([("192.168.1.50", 40001)])
        announce = self._tick([("81.2.3.4", 40001)])
        announce.assert_called_once()
        self.assertEqual(network_change._last_known_addresses, frozenset({("81.2.3.4", 40001)}))

    def test_disabled_never_checks_or_announces(self):
        announce = self._tick([("81.2.3.4", 40001)], enabled=False)
        announce.assert_not_called()
        self.assertIsNone(network_change._last_known_addresses)

    def test_a_tick_inside_the_interval_is_a_no_op(self):
        with patch.object(network_change, "_is_enabled", return_value=True), \
             patch.object(network_change, "_check_interval_seconds", return_value=60.0), \
             patch.object(
                 network_change.gateway_utils,
                 "_uris_for_all_interfaces",
                 side_effect=[
                     [_uri("192.168.1.50", 40001)],
                     [_uri("81.2.3.4", 40001)],
                 ],
             ) as uris, \
             patch("time.monotonic", side_effect=[100.0, 100.5]), \
             patch.object(network_change, "_announce_to_known_peers") as announce:
            network_change.network_change_tick()
            network_change.network_change_tick()

        # The second call landed inside the interval, so the address was never
        # recomputed a second time and nothing was announced.
        self.assertEqual(uris.call_count, 1)
        announce.assert_not_called()

    def test_a_tick_never_raises(self):
        with patch.object(network_change, "_is_enabled", side_effect=RuntimeError("boom")):
            network_change.network_change_tick()  # must not raise


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AnnounceToKnownPeersTests(unittest.TestCase):
    def test_a_peer_with_no_reachable_address_is_skipped_and_the_rest_still_run(self):
        channel = MagicMock()
        result = MagicMock(token="OK")

        with patch.object(network_change.sc, "get_peers_id", return_value=["peer-a", "peer-b"]), \
             patch.object(network_change, "generate_full_node_peer_info", return_value=MagicMock()), \
             patch.object(network_change, "get_client_id_on_other_peer", return_value="client-1"), \
             patch.object(
                 network_change, "peer_channel",
                 side_effect=[ConnectionError("no known address"), channel],
             ), \
             patch.object(network_change.BeeClient, "introduce_peer", return_value=result) as introduce:
            network_change._announce_to_known_peers()

        introduce.assert_called_once()
        channel.close.assert_called_once()

    def test_a_client_id_failure_skips_that_peer_without_opening_a_channel(self):
        with patch.object(network_change.sc, "get_peers_id", return_value=["peer-a"]), \
             patch.object(network_change, "generate_full_node_peer_info", return_value=MagicMock()), \
             patch.object(network_change, "get_client_id_on_other_peer", side_effect=Exception("not available")), \
             patch.object(network_change, "peer_channel") as peer_channel, \
             patch.object(network_change.BeeClient, "introduce_peer") as introduce:
            network_change._announce_to_known_peers()

        peer_channel.assert_not_called()
        introduce.assert_not_called()

    def test_no_known_peers_does_nothing(self):
        with patch.object(network_change.sc, "get_peers_id", return_value=[]), \
             patch.object(network_change, "generate_full_node_peer_info") as generate:
            network_change._announce_to_known_peers()

        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

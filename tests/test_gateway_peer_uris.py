"""Regression test: a Docker/veth bridge address must never be announced to a peer.

``nodo connect`` to a LAN peer used to come back with the peer's real LAN IP
(e.g. 192.168.1.47). After the multi-address refactor (issue #236) it started
returning Docker-created interface addresses too (172.17.0.1, 172.18.x.x),
because ``_uris_for_all_interfaces`` enumerated every interface with no filter.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    import src.gateway.utils as gateway_utils
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    gateway_utils = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UrisForAllInterfacesTests(unittest.TestCase):
    def test_docker_and_veth_interfaces_are_never_announced(self):
        interfaces = {
            "lo": "127.0.0.1",
            "eth0": "192.168.1.47",
            "docker0": "172.17.0.1",
            "br-abc123": "172.18.0.1",
            "veth9f8e7d": "172.19.0.5",
        }

        # Interface filtering is independent of the boot-time firewall/port assignment.
        with patch.object(gateway_utils, "_gateway_port", return_value=40001), \
             patch.object(gateway_utils, "_public_host", return_value=None), \
             patch.object(gateway_utils.ni, "interfaces", return_value=list(interfaces)), \
             patch.object(
                 gateway_utils,
                 "get_local_ip_from_network",
                 side_effect=lambda interface, allow_link_local=False: interfaces[interface],
             ):
            uris = gateway_utils._uris_for_all_interfaces()

        announced_ips = {uri.ip for uri in uris}
        self.assertEqual(announced_ips, {"192.168.1.47"})

    def _announced(self, interfaces, **settings):
        real_get = gateway_utils.env_manager.get

        def get(key, default=None):
            return settings.get(key, real_get(key, default))

        with patch.object(gateway_utils, "_gateway_port", return_value=40001), \
             patch.object(gateway_utils, "_public_host", return_value=None), \
             patch.object(gateway_utils.env_manager, "get", side_effect=get), \
             patch.object(gateway_utils.ni, "interfaces", return_value=list(interfaces)), \
             patch.object(
                 gateway_utils,
                 "get_local_ip_from_network",
                 side_effect=lambda interface, allow_link_local=False: interfaces[interface],
             ):
            return [uri.ip for uri in gateway_utils._uris_for_all_interfaces()]

    def test_the_guest_bridge_is_never_announced(self):
        # #655: the bridge of nodo for its guests is not a docker or a veth interface.
        interfaces = {"lo": "127.0.0.1", "nodo-br-ch": "192.168.200.1", "host0": "10.213.195.2"}
        self.assertEqual(self._announced(interfaces), ["10.213.195.2"])

    def test_a_renamed_bridge_is_not_announced_by_its_name(self):
        interfaces = {"guests0": "10.77.0.1", "eth0": "192.168.1.47"}
        self.assertEqual(self._announced(
            interfaces, **{"virtualizers.ch.NETWORK_BRIDGE_NAME": "guests0"}), ["192.168.1.47"])

    def test_an_address_in_the_guest_subnet_is_not_announced(self):
        interfaces = {"eth1": "192.168.200.1", "eth0": "192.168.1.47"}
        self.assertEqual(self._announced(interfaces), ["192.168.1.47"])

    def test_a_node_with_only_the_guest_bridge_announces_nothing(self):
        self.assertEqual(self._announced({"nodo-br-ch": "192.168.200.1"}), [])

    def test_the_invitation_leaves_the_guest_bridge_out(self):
        # The path of `nodo invite`: the Peer of the node, and the invitation made from it.
        from src.utils import peer_invite

        interfaces = {"nodo-br-ch": "192.168.200.1", "host0": "10.213.195.2"}
        with patch.object(gateway_utils, "_gateway_port", return_value=40001), \
             patch.object(gateway_utils, "_public_host", return_value=None), \
             patch.object(gateway_utils, "_local_payment_contracts", return_value=[]), \
             patch.object(gateway_utils.ni, "interfaces", return_value=list(interfaces)), \
             patch.object(
                 gateway_utils,
                 "get_local_ip_from_network",
                 side_effect=lambda interface, allow_link_local=False: interfaces[interface],
             ):
            peer = gateway_utils.generate_full_node_peer_info(fresh=True)
        targets = peer_invite.invite_targets(
            peer_invite.parse_invite(peer_invite.build_invite(peer)))
        self.assertEqual(targets, ["10.213.195.2:40001"])

    def test_does_not_fall_back_to_loopback_when_no_address_is_usable(self):
        with patch.object(gateway_utils, "_public_host", return_value=None), \
             patch.object(gateway_utils.ni, "interfaces", return_value=[]):
            uris = gateway_utils._uris_for_all_interfaces()

        self.assertEqual(uris, [])


if __name__ == "__main__":
    unittest.main()

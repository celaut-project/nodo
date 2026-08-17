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

        with patch.object(gateway_utils, "_public_host", return_value=None), \
             patch.object(gateway_utils.ni, "interfaces", return_value=list(interfaces)), \
             patch.object(
                 gateway_utils,
                 "get_local_ip_from_network",
                 side_effect=lambda interface, allow_link_local=False: interfaces[interface],
             ):
            uris = gateway_utils._uris_for_all_interfaces()

        announced_ips = {uri.ip for uri in uris}
        self.assertEqual(announced_ips, {"192.168.1.47"})

    def test_does_not_fall_back_to_loopback_when_no_address_is_usable(self):
        with patch.object(gateway_utils, "_public_host", return_value=None), \
             patch.object(gateway_utils.ni, "interfaces", return_value=[]):
            uris = gateway_utils._uris_for_all_interfaces()

        self.assertEqual(uris, [])


if __name__ == "__main__":
    unittest.main()

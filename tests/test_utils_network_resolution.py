import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    import src.utils.utils as utils
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    utils = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UtilsNetworkResolutionTests(unittest.TestCase):
    def test_get_local_ip_from_network_uses_ipv6_when_ipv4_is_missing(self):
        with patch.object(
            utils.ni,
            "ifaddresses",
            return_value={utils.ni.AF_INET6: [{"addr": "fe80::1234%eth0"}]},
        ):
            ip = utils.get_local_ip_from_network("eth0", allow_link_local=True)

        self.assertEqual(ip, "fe80::1234")

    def test_get_local_ip_from_network_rejects_link_local_when_not_allowed(self):
        with patch.object(
            utils.ni,
            "ifaddresses",
            return_value={utils.ni.AF_INET6: [{"addr": "fe80::1234%eth0"}]},
        ):
            with self.assertRaisesRegex(KeyError, "without link-local"):
                utils.get_local_ip_from_network("eth0", allow_link_local=False)

    def test_is_virtual_interface_flags_container_and_vpn_interfaces(self):
        for name in ("docker0", "br-abc123", "veth1234", "virbr0", "tailscale0", "wg0"):
            self.assertTrue(utils.is_virtual_interface(name), name)

    def test_is_virtual_interface_leaves_real_interfaces_alone(self):
        for name in ("eth0", "wlan0", "en0", "lo"):
            self.assertFalse(utils.is_virtual_interface(name), name)

    def test_get_network_name_handles_raw_ipv6_without_truncating_on_colon(self):
        with patch.object(utils.ni, "interfaces", return_value=["eth0"]), patch.object(
            utils,
            "__address_in_network",
            side_effect=lambda ip_or_uri, net: ip_or_uri == "5B::1" and net == "eth0",
        ):
            network = utils.get_network_name("5B::1%5")

        self.assertEqual(network, "eth0")


if __name__ == "__main__":
    unittest.main()

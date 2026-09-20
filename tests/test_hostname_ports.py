"""A hostname tag opens the ports its network's ``protocol_stack`` names (#389).

``resolve_domain`` opened 80 and 443, hardcoded, with a TODO saying the ports
should come from the protocol stack. Now they do, with the old pair as the fallback
for a stack that names none -- which is every service published before this.
"""
import unittest
from unittest.mock import patch

try:
    from tests.config_bootstrap import load_example_config

    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.identity.node_identity import component_formal
    from tests.test_pow_networks import _load_networks_module

    nets = _load_networks_module()
    IMPORT_ERROR = None if nets is not None else RuntimeError("networks.py did not load")
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = None
    nets = None


def _proto(*tags, formal=None):
    p = celaut.Service.Api.Protocol(tags=list(tags))
    if formal is not None:
        p.formal = component_formal(formal)
    return p


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HostnamePortsTests(unittest.TestCase):
    def test_an_empty_stack_keeps_the_historical_pair(self):
        self.assertEqual(nets.hostname_ports([]), [80, 443])

    def test_a_port_in_the_formal_wins(self):
        self.assertEqual(nets.hostname_ports([_proto("grpc", formal={"port": "9053"})]), [9053])

    def test_a_well_known_tag_supplies_its_port(self):
        self.assertEqual(nets.hostname_ports([_proto("https")]), [443])
        self.assertEqual(nets.hostname_ports([_proto("http")]), [80])
        self.assertEqual(nets.hostname_ports([_proto("ssh")]), [22])

    def test_each_entry_contributes_and_duplicates_collapse(self):
        stack = [_proto("http"), _proto("https"), _proto("tls", formal={"port": "443"})]
        self.assertEqual(nets.hostname_ports(stack), [80, 443])

    def test_an_entry_naming_no_port_contributes_nothing(self):
        self.assertEqual(nets.hostname_ports([_proto("grpc"), _proto("https")]), [443])

    def test_a_stack_naming_no_port_at_all_falls_back(self):
        self.assertEqual(nets.hostname_ports([_proto("grpc")]), [80, 443])

    def test_a_bad_formal_or_port_is_ignored_not_raised(self):
        bad = celaut.Service.Api.Protocol(tags=["https"], formal=b"\xff\xfe not lines")
        self.assertEqual(nets.hostname_ports([bad]), [443])
        self.assertEqual(nets.hostname_ports([_proto("x", formal={"port": "abc"})]), [80, 443])
        self.assertEqual(nets.hostname_ports([_proto("x", formal={"port": "70000"})]), [80, 443])


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveDomainUsesThePortsTests(unittest.TestCase):
    def _addrinfo(self, host, port, *a):
        return [(nets.socket.AF_INET, None, None, None, ("203.0.113.1", 0))]

    def test_a_hostname_network_with_a_stack_opens_only_those_ports(self):
        network = celaut.Service.Network(
            tags=["api.example.test"],
            protocol_stack=[_proto("grpc", formal={"port": "50051"})],
        )
        with patch.object(nets.socket, "getaddrinfo", side_effect=self._addrinfo):
            peers = nets.resolve_network(network)
        self.assertEqual(
            [(u.ip, u.port) for u in peers[0].uri_slot[0].uri], [("203.0.113.1", 50051)]
        )

    def test_a_bare_hostname_network_opens_80_and_443_as_before(self):
        with patch.object(nets.socket, "getaddrinfo", side_effect=self._addrinfo):
            peers = nets.resolve_network(celaut.Service.Network(tags=["www.example.test"]))
        self.assertEqual(
            sorted(u.port for u in peers[0].uri_slot[0].uri), [80, 443]
        )


if __name__ == "__main__":
    unittest.main()

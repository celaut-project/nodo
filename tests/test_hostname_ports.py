"""A hostname tag opens the port its network entry states, and nothing else does (#389).

``resolve_domain`` opened 80 and 443, hardcoded, with a TODO saying the ports should
come from the protocol stack. They do not, and no declaration chooses a peer's port in
general: every other peer this module resolves is an instance published on the port the
node running it assigned. A hostname is the exception -- there is no instance and no
node that published one -- so its entry may state the standard port the name answers
on, as ``port=<n>`` in the entry's ``formal``.

An entry stating none opens the historical pair; one whose port cannot be read opens
nothing at all.
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


def _network(*tags, formal=None, stack=()):
    network = celaut.Service.Network(
        tags=list(tags) or ["api.example.test"],
        protocol_stack=[celaut.Service.Api.Protocol(tags=[t]) for t in stack],
    )
    if formal is not None:
        network.formal = component_formal(formal)
    return network


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HostnamePortsTests(unittest.TestCase):
    def test_an_entry_without_a_formal_keeps_the_historical_pair(self):
        self.assertEqual(nets.hostname_ports(_network()), [80, 443])

    def test_a_formal_saying_nothing_about_ports_keeps_the_historical_pair(self):
        self.assertEqual(nets.hostname_ports(_network(formal={"api.version": "4"})), [80, 443])

    def test_the_port_comes_from_the_entrys_formal(self):
        self.assertEqual(nets.hostname_ports(_network(formal={"port": "9053"})), [9053])

    def test_a_protocol_stack_says_nothing_about_ports(self):
        """Which protocols the peers speak is not where a port lives."""
        self.assertEqual(nets.hostname_ports(_network(stack=("https", "tls"))), [80, 443])
        self.assertEqual(
            nets.hostname_ports(_network(formal={"port": "8443"}, stack=("https",))), [8443]
        )

    def test_a_port_that_is_not_a_port_number_grants_nothing(self):
        """Not the historical pair: that would open what the author never declared."""
        for value in ("abc", "70000", "0", "-1", "${PORT}", "80.0"):
            with patch.object(nets, "LOGGER"):
                self.assertEqual(nets.hostname_ports(_network(formal={"port": value})), [], value)

    def test_an_unreadable_formal_grants_nothing(self):
        network = celaut.Service.Network(tags=["api.example.test"], formal=b"\xff\xfe not lines")
        with patch.object(nets, "LOGGER"):
            self.assertEqual(nets.hostname_ports(network), [])


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveDomainUsesThePortTests(unittest.TestCase):
    def _addrinfo(self, host, port, *a):
        return [(nets.socket.AF_INET, None, None, None, ("203.0.113.1", 0))]

    def test_a_hostname_entry_stating_a_port_opens_only_that_one(self):
        with patch.object(nets.socket, "getaddrinfo", side_effect=self._addrinfo):
            peers = nets.resolve_network(_network(formal={"port": "50051"}))
        self.assertEqual(
            [(u.ip, u.port) for u in peers[0].uri_slot[0].uri], [("203.0.113.1", 50051)]
        )

    def test_a_bare_hostname_network_opens_80_and_443_as_before(self):
        with patch.object(nets.socket, "getaddrinfo", side_effect=self._addrinfo):
            peers = nets.resolve_network(celaut.Service.Network(tags=["www.example.test"]))
        self.assertEqual(
            sorted(u.port for u in peers[0].uri_slot[0].uri), [80, 443]
        )

    def test_an_unreadable_port_grants_no_peers_and_is_not_even_looked_up(self):
        network = _network(formal={"port": "nine thousand"})
        with patch.object(nets.socket, "getaddrinfo", side_effect=self._addrinfo) as dns, \
                patch.object(nets, "LOGGER"):
            self.assertEqual(nets.resolve_network(network), [])
        dns.assert_not_called()


if __name__ == "__main__":
    unittest.main()

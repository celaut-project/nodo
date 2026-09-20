"""Every tag of a ``Service.Network`` is resolved; peers accumulate (#390).

Before this, ``resolve_network`` stopped at the first tag that produced anything, so
``["google.com", "www.google.com"]`` granted the first name only -- silently. The
operator's policy has always read a network as *as many destinations as it names*
(``docs/NETWORKS.md``, "Every tag must pass"); the resolver now agrees with it.

Loaded the way ``test_pow_networks.py`` loads the module, so the database seam is a
stub and DNS is patched.
"""
import unittest
from unittest.mock import patch

try:
    from tests.config_bootstrap import load_example_config

    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.manager import network_defaults as defaults
    from tests.test_pow_networks import _load_networks_module

    nets = _load_networks_module()
    IMPORT_ERROR = None if nets is not None else RuntimeError("networks.py did not load")
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = None
    nets = None


def _uri(ip, port=443):
    return celaut.Instance.Uri(ip=ip, port=port)


def _ips(peers):
    return [[(u.ip, u.port) for u in p.uri_slot[0].uri] for p in peers]


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AllTagsAreResolvedTests(unittest.TestCase):
    def test_two_hostnames_in_one_entry_are_two_peers(self):
        answers = {"google.com": [_uri("142.250.0.1")], "www.google.com": [_uri("142.250.0.2")]}
        with patch.object(nets, "resolve_domain", side_effect=lambda t: answers[t]) as dns:
            peers = nets.resolve_network(
                celaut.Service.Network(tags=["google.com", "www.google.com"])
            )
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(_ips(peers), [[("142.250.0.1", 443)], [("142.250.0.2", 443)]])

    def test_one_hostname_with_several_records_is_one_peer_at_several_addresses(self):
        with patch.object(
            nets, "resolve_domain", return_value=[_uri("203.0.113.1"), _uri("203.0.113.2")]
        ):
            peers = nets.resolve_network(celaut.Service.Network(tags=["example.test"]))
        self.assertEqual(len(peers), 1)
        self.assertEqual(_ips(peers), [[("203.0.113.1", 443), ("203.0.113.2", 443)]])

    def test_tags_naming_no_peer_do_not_hide_the_ones_that_do(self):
        """``["ipv4", "public", "api.example.test"]``: the hostname is still resolved."""
        with patch.object(nets, "resolve_domain", return_value=[_uri("203.0.113.9")]) as dns:
            peers = nets.resolve_network(
                celaut.Service.Network(tags=["ipv4", "public", "api.example.test"])
            )
        dns.assert_called_once_with("api.example.test")
        self.assertEqual(_ips(peers), [[("203.0.113.9", 443)]])

    def test_operator_seeds_and_dns_accumulate_across_tags(self):
        def seeds(tag, config=None):
            return ["tcp://10.0.0.1:9000"] if tag == "seeded.test" else []

        with patch.object(nets, "configured_endpoints", side_effect=seeds), patch.object(
            defaults.socket, "getaddrinfo",
            side_effect=lambda host, port, *a: [(None, None, None, None, (host, port))],
        ), patch.object(nets, "resolve_domain", return_value=[_uri("203.0.113.5")]) as dns:
            peers = nets.resolve_network(
                celaut.Service.Network(tags=["seeded.test", "dns.test"])
            )
        dns.assert_called_once_with("dns.test")
        self.assertEqual(_ips(peers), [[("10.0.0.1", 9000)], [("203.0.113.5", 443)]])

    def test_a_seeded_tag_is_not_also_looked_up_in_dns(self):
        with patch.object(
            nets, "configured_endpoints", return_value=["tcp://10.0.0.1:9000"]
        ), patch.object(
            defaults.socket, "getaddrinfo",
            side_effect=lambda host, port, *a: [(None, None, None, None, (host, port))],
        ), patch.object(nets, "resolve_domain") as dns:
            nets.resolve_network(celaut.Service.Network(tags=["seeded.test"]))
        dns.assert_not_called()

    def test_pow_peers_accumulate_with_a_dns_tag(self):
        pow_peer = celaut.Instance(uri_slot=[celaut.Instance.Uri_Slot(
            internal_port=1, uri=[_uri("198.51.100.1", 9053)]
        )])
        with patch.object(
            nets, "resolve_pow_network", return_value=[pow_peer]
        ), patch.object(nets, "resolve_domain", return_value=[_uri("203.0.113.7")]):
            peers = nets.resolve_network(
                celaut.Service.Network(tags=["pow:ergo", "explorer.test"])
            )
        self.assertEqual(_ips(peers), [[("198.51.100.1", 9053)], [("203.0.113.7", 443)]])

    def test_the_wildcard_and_a_single_tag_are_unchanged(self):
        with patch.object(nets, "resolve_domain") as dns:
            self.assertEqual(nets.resolve_network(celaut.Service.Network(tags=["*"])), [])
        dns.assert_not_called()

    def test_a_partial_answer_is_logged_and_a_full_one_is_not(self):
        answers = {"a.test": [_uri("203.0.113.1")], "b.test": []}
        with patch.object(nets, "resolve_domain", side_effect=lambda t: answers[t]), \
             patch.object(nets, "LOGGER") as logger:
            nets.resolve_network(celaut.Service.Network(tags=["a.test", "b.test"]))
        self.assertTrue(logger.called)
        self.assertIn("1 of 2", logger.call_args.args[0])

        answers["b.test"] = [_uri("203.0.113.2")]
        with patch.object(nets, "resolve_domain", side_effect=lambda t: answers[t]), \
             patch.object(nets, "LOGGER") as logger:
            nets.resolve_network(celaut.Service.Network(tags=["a.test", "b.test"]))
        logger.assert_not_called()

    def test_the_environment_filter_applies_to_the_accumulated_set(self):
        network = celaut.Service.Network(
            tags=["a.test", "b.test"], environment_variable="CLUSTER"
        )
        answers = {"a.test": [_uri("203.0.113.1")], "b.test": [_uri("203.0.113.2")]}
        lookup = lambda peer: {"CLUSTER": b"x"} if peer.uri_slot[0].uri[0].ip.endswith(".2") else {}
        with patch.object(nets, "resolve_domain", side_effect=lambda t: answers[t]):
            peers = nets.resolve_network(
                network, requester_env_values={"CLUSTER": b"x"}, peer_env_lookup=lookup
            )
        self.assertEqual(_ips(peers), [[("203.0.113.2", 443)]])


if __name__ == "__main__":
    unittest.main()

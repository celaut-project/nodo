"""Wildcard hostname tags: refused at pack time, survivable at launch (#391).

``*.googlevideo.com`` is lowercase and dotted, so it passed ``resolve_network``'s
hostname heuristic and reached ``resolve_domain``, which raised ``Cannot resolve
domain`` -- and nothing caught it before the launcher's catch-all tore the VM down.
The author saw a DNS error naming a host nobody meant literally.

Two changes, each tested here: the packer refuses the shape where the author can see
it, and the resolver treats *any* unresolvable tag as "no peers for this one" with a
log line, instead of failing the launch.
"""
import unittest
from unittest.mock import patch

try:
    from tests.config_bootstrap import load_example_config

    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.packers.zip_with_dockerfile import ZipContainerPacker
    from tests.test_pow_networks import _load_networks_module

    nets = _load_networks_module()
    IMPORT_ERROR = None if nets is not None else RuntimeError("networks.py did not load")
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = None
    nets = None
    ZipContainerPacker = None


def _networks(service_json):
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = service_json
    return packer._parsed_networks()


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PackerRefusesWildcardHostnamesTests(unittest.TestCase):
    def test_a_star_dot_hostname_fails_the_pack_naming_the_tag(self):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": [
                {"tags": ["*.googlevideo.com"], "prose": "youtube's CDN"},
            ]})
        self.assertIn("*.googlevideo.com", str(caught.exception))
        self.assertIn("wildcard hostname", str(caught.exception))
        self.assertIn("network[0]", str(caught.exception))

    def test_the_index_names_the_offending_entry(self):
        with self.assertRaises(ValueError) as caught:
            _networks({"network": [
                {"tags": ["www.youtube.com"], "prose": "fine"},
                {"tags": ["*.example.test"], "prose": "not fine"},
            ]})
        self.assertIn("network[1]", str(caught.exception))

    def test_the_bare_wildcard_is_still_open_egress(self):
        [network] = _networks({"network": [{"tags": ["*"], "prose": "everything"}]})
        self.assertEqual(list(network.tags), ["*"])

    def test_concrete_hostnames_and_labels_still_pack(self):
        [network] = _networks({"network": [
            {"tags": ["ipv4", "public", "www.youtube.com"], "prose": "ok"},
        ]})
        self.assertEqual(list(network.tags), ["ipv4", "public", "www.youtube.com"])


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UnresolvableTagsDoNotFailTheLaunchTests(unittest.TestCase):
    def test_a_wildcard_tag_in_an_old_pack_yields_no_peers_and_is_named_as_a_glob(self):
        with patch.object(
            nets, "resolve_domain", side_effect=ValueError("Cannot resolve domain: *.cdn.test")
        ), patch.object(nets, "LOGGER") as logger:
            peers = nets.resolve_network(celaut.Service.Network(tags=["*.cdn.test"]))
        self.assertEqual(peers, [])
        self.assertIn("wildcard hostname", logger.call_args.args[0])
        self.assertIn("*.cdn.test", logger.call_args.args[0])

    def test_a_hostname_that_does_not_resolve_right_now_yields_no_peers(self):
        with patch.object(
            nets, "resolve_domain", side_effect=ValueError("Cannot resolve domain: down.test")
        ), patch.object(nets, "LOGGER") as logger:
            peers = nets.resolve_network(celaut.Service.Network(tags=["down.test"]))
        self.assertEqual(peers, [])
        self.assertIn("did not resolve", logger.call_args.args[0])

    def test_a_later_tag_is_still_tried_after_an_unresolvable_one(self):
        def dns(tag):
            if tag == "down.test":
                raise ValueError("Cannot resolve domain: down.test")
            return [celaut.Instance.Uri(ip="203.0.113.4", port=443)]

        with patch.object(nets, "resolve_domain", side_effect=dns), patch.object(nets, "LOGGER"):
            peers = nets.resolve_network(celaut.Service.Network(tags=["down.test", "up.test"]))
        self.assertEqual(
            [(u.ip, u.port) for u in peers[0].uri_slot[0].uri], [("203.0.113.4", 443)]
        )

    def test_a_resolving_hostname_is_untouched(self):
        with patch.object(
            nets, "resolve_domain", return_value=[celaut.Instance.Uri(ip="203.0.113.2", port=443)]
        ), patch.object(nets, "LOGGER") as logger:
            peers = nets.resolve_network(celaut.Service.Network(tags=["example.test"]))
        self.assertEqual(len(peers), 1)
        logger.assert_not_called()


if __name__ == "__main__":
    unittest.main()

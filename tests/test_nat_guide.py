"""Tests for ``nodo nat-guide`` (``src/commands/nat_guide.py``).

The guide's text is a pure function of detected facts, so the wording is tested
without root, a router or a network. What matters most: a fact that could not be
detected must be *omitted*, never invented — a guide that confidently prints the
wrong router address is worse than one that says it could not tell.
"""

import unittest

IMPORT_ERROR = None
try:
    from src.commands import nat_guide
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    nat_guide = None  # type: ignore[assignment]


def _facts(**overrides) -> dict:
    facts = {
        "gateway_port": 8090,
        "local_ip": "192.168.1.34",
        "router_ip": "192.168.1.1",
        "listening": True,
        "ddns_enabled": True,
        "ddns_hostname": "my-node.dedyn.io",
        "ddns_resolves_to": "203.0.113.7",
        "direct_exposure": True,
        "free_ports_range": [{"START": 50000, "END": 60000}],
    }
    facts.update(overrides)
    return facts


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DefaultGatewayParsingTests(unittest.TestCase):
    def test_a_normal_default_route_yields_the_router_address(self):
        output = (
            "default via 192.168.1.1 dev wlp3s0 proto dhcp src 192.168.1.34 metric 600"
        )
        self.assertEqual(nat_guide.parse_default_gateway(output), "192.168.1.1")

    def test_the_default_route_is_found_among_other_routes(self):
        output = (
            "10.0.0.0/24 dev br-ch proto kernel scope link src 10.0.0.1\n"
            "default via 10.9.9.254 dev eth0 proto static metric 100\n"
        )
        self.assertEqual(nat_guide.parse_default_gateway(output), "10.9.9.254")

    def test_no_default_route_yields_none(self):
        self.assertIsNone(nat_guide.parse_default_gateway(""))
        self.assertIsNone(
            nat_guide.parse_default_gateway("10.0.0.0/24 dev br-ch scope link")
        )

    def test_unexpected_output_yields_none_rather_than_a_wrong_address(self):
        # An IPv6-only answer and a different tool's output must both fail closed.
        self.assertIsNone(nat_guide.parse_default_gateway("default dev eth0 scope link"))
        self.assertIsNone(nat_guide.parse_default_gateway("Kernel IP routing table"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuideRenderingTests(unittest.TestCase):
    def test_the_forwarding_rule_uses_this_host_and_port(self):
        guide = nat_guide.render_guide(_facts())

        self.assertIn("External port: 8090", guide)
        self.assertIn("Internal host: 192.168.1.34", guide)
        self.assertIn("Internal port: 8090", guide)
        self.assertIn("http://192.168.1.1/", guide)

    def test_it_says_one_port_is_enough_thanks_to_tunneling(self):
        guide = nat_guide.render_guide(_facts())

        self.assertIn("do NOT need a port per service", guide)

    def test_an_undetected_router_is_admitted_not_invented(self):
        guide = nat_guide.render_guide(_facts(router_ip=None))

        self.assertIn("could not detect a default gateway", guide)
        self.assertNotIn("http://None/", guide)

    def test_an_undetected_local_ip_is_admitted_not_invented(self):
        guide = nat_guide.render_guide(_facts(local_ip=None))

        self.assertIn("could not detect", guide)
        self.assertIn("Internal host: <this machine>", guide)
        self.assertNotIn("None", guide.split("On your router:")[1])

    def test_a_missing_gateway_port_is_called_out(self):
        guide = nat_guide.render_guide(_facts(gateway_port=None))

        self.assertIn("not resolvable", guide)
        self.assertIn("<gateway port>", guide)

    def test_a_stopped_node_is_reported(self):
        guide = nat_guide.render_guide(_facts(listening=False))

        self.assertIn("nothing listening locally", guide)

    def test_an_undeterminable_listen_state_is_simply_absent(self):
        guide = nat_guide.render_guide(_facts(listening=None))

        self.assertNotIn("listening locally", guide)

    def test_ddns_enabled_puts_the_hostname_in_the_check_command(self):
        guide = nat_guide.render_guide(_facts())

        self.assertIn("nc -vz my-node.dedyn.io 8090", guide)
        self.assertIn("resolves to 203.0.113.7", guide)

    def test_ddns_disabled_says_peers_face_a_changing_ip(self):
        guide = nat_guide.render_guide(_facts(ddns_enabled=False))

        self.assertIn("DDNS is disabled", guide)
        self.assertIn("<your public IP>", guide)

    def test_ddns_that_does_not_resolve_is_reported(self):
        guide = nat_guide.render_guide(_facts(ddns_resolves_to=None))

        self.assertIn("does not resolve yet", guide)

    def test_the_free_ports_range_is_only_mentioned_for_direct_exposure(self):
        with_direct = nat_guide.render_guide(_facts())
        self.assertIn("50000-60000", with_direct)

        tunnel_only = nat_guide.render_guide(_facts(direct_exposure=False))
        self.assertNotIn("50000-60000", tunnel_only)

    def test_it_warns_that_testing_from_inside_proves_nothing(self):
        guide = nat_guide.render_guide(_facts())

        self.assertIn("from inside your own network", guide)

    def test_cgnat_is_called_out_as_unfixable_on_the_router(self):
        guide = nat_guide.render_guide(_facts())

        self.assertIn("CGNAT", guide)

    def test_a_malformed_port_range_is_skipped_quietly(self):
        guide = nat_guide.render_guide(
            _facts(free_ports_range=[{"START": "x", "END": 60000}, {"nope": 1}])
        )

        # Nothing to advertise, so the direct-exposure line is dropped entirely.
        self.assertNotIn("forward , as well", guide)
        self.assertNotIn("None-None", guide)


if __name__ == "__main__":
    unittest.main()

"""Tests for the prices a node advertises to its peers.

The rates ride in ``Peer.mu_per_call`` -- node-wide, not per-address, since
they do not depend on which of a node's addresses you reach it through. A receiving
peer stores the whole message verbatim in ``peer.advertisement``
(``manager.add_peer_instance``) and ``submit_to_ledger`` republishes it for the
reputation JSON. So the test that matters is the round trip: the rates have to
survive being serialized and parsed back the way those two paths do it, or the whole
mechanism is decoration.

Every rate is in MU, and MU is pegged (1 MU = 1 nanoERG), which is what makes a rate
actionable to the peer reading it. The model this replaced advertised an undefined
"gas", so the numbers meant nothing off this node.
"""

import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from google.protobuf.json_format import MessageToJson

    from protos import celaut_pb2 as celaut
    from src.utils.config import ConfigManager
    from src.utils.cost_functions import general_cost_functions as rates_module
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    rates_module = None  # type: ignore[assignment]
    ConfigManager = None  # type: ignore[assignment]


def _config(**overrides):
    """Override only the pricing keys; ConfigManager is a shared singleton."""
    values = {
        "pricing.RAM_ERG_PER_GIB_HOUR": "0.001",
        "pricing.CPU_ERG_PER_VCPU_HOUR": "0.004",
        "pricing.DISK_ERG_PER_GIB_HOUR": "0.0001",
        "pricing.NET_ERG_PER_GIB": "0.002",
        "pricing.BUILD_ERG": "0.01",
        "pricing.TUNNEL_OPEN_ERG": "0.00001",
        "pricing.MODIFY_RESOURCES_ERG": "0.00001",
        "pricing.SCARCITY_MAX_MULTIPLIER": 10,
        "pricing.SCARCITY_CURVE": 1.0,
    }
    values.update(overrides)
    # Patch the singleton itself: pricing resolves ConfigManager() per call, so a
    # module-level reference would be the wrong object depending on import order.
    manager = ConfigManager()
    real_get = manager.get

    def get(key, default=None):
        return values[key] if key in values else real_get(key, default)

    return patch.object(manager, "get", side_effect=get)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NodeAdvertisedRatesTests(unittest.TestCase):
    def test_every_priced_resource_is_advertised_separately(self):
        with _config():
            advertised = rates_module.node_advertised_rates()

        self.assertEqual(
            advertised,
            {
                # 0.001 ERG per GiB-hour == 1e6 MU / 3600s.
                "ram_mu_per_gib_second": 277,
                "cpu_mu_per_vcpu_second": 1111,
                "disk_mu_per_gib_second": 27,
                "net_mu_per_gib": 2_000_000,
                "build_mu": 10_000_000,
                "tunnel_open_mu": 10_000,
                "scarcity_max_multiplier": 10,
            },
        )

    def test_resources_are_priced_independently(self):
        """A node short on memory can charge for it without touching disk.

        This is the property the single `EXECUTION_COST` scalar could not express.
        """
        with _config(**{"pricing.RAM_ERG_PER_GIB_HOUR": "0.1"}):
            advertised = rates_module.node_advertised_rates()

        self.assertEqual(advertised["ram_mu_per_gib_second"], 27_777)
        self.assertEqual(advertised["disk_mu_per_gib_second"], 27)

    def test_a_zero_price_is_omitted_rather_than_advertised_as_free(self):
        with _config(**{"pricing.TUNNEL_OPEN_ERG": "0"}):
            advertised = rates_module.node_advertised_rates()

        self.assertNotIn("tunnel_open_mu", advertised)
        self.assertIn("net_mu_per_gib", advertised)

    def test_a_malformed_price_is_rejected_not_silently_zeroed(self):
        """Reading a broken price as 0 would give the node's resources away."""
        with _config(**{"pricing.RAM_ERG_PER_GIB_HOUR": "cheap"}):
            with self.assertRaises(ValueError):
                rates_module.node_advertised_rates()

    def test_rates_below_one_mu_per_second_are_omitted(self):
        """An integer per-second rate cannot express a fraction; 0 would read as free."""
        with _config(**{"pricing.DISK_ERG_PER_GIB_HOUR": "0.000000001"}):
            advertised = rates_module.node_advertised_rates()

        self.assertNotIn("disk_mu_per_gib_second", advertised)
        self.assertIn("ram_mu_per_gib_second", advertised)

    def test_the_scarcity_ceiling_is_advertised_with_the_base_prices(self):
        """A base price alone does not bound what a peer may be charged."""
        with _config(**{"pricing.SCARCITY_MAX_MULTIPLIER": 4}):
            advertised = rates_module.node_advertised_rates()

        self.assertEqual(advertised["scarcity_max_multiplier"], 4)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AdvertisedRatesSurviveTheWireTests(unittest.TestCase):
    """The rates must survive the two paths a peer's advertisement goes through.

    They are node-wide, so they live on ``Peer`` itself rather than on any one
    address: ``add_peer_instance`` stores the whole serialized ``Peer`` in
    ``peer.advertisement``, and ``peers.py`` / ``submit_to_ledger`` read it back.
    """

    def _peer_with_rates(self) -> "celaut.Peer":
        peer = celaut.Peer()
        uri = peer.uri.add(ip="1.2.3.4", port=8090)
        uri.transport.tags.append("tcp")
        with _config():
            for rate, amount_mu in rates_module.node_advertised_rates().items():
                peer.mu_per_call[rate].n = str(amount_mu)
        return peer

    def test_rates_survive_the_advertisement_round_trip(self):
        """add_peer_instance stores the Peer bytes; peers.py parses them back."""
        stored = self._peer_with_rates().SerializeToString()

        parsed = celaut.Peer()
        parsed.ParseFromString(stored)

        self.assertEqual(
            {rate: amount.n for rate, amount in parsed.mu_per_call.items()},
            {
                "ram_mu_per_gib_second": "277",
                "cpu_mu_per_vcpu_second": "1111",
                "disk_mu_per_gib_second": "27",
                "net_mu_per_gib": "2000000",
                "build_mu": "10000000",
                "tunnel_open_mu": "10000",
                "scarcity_max_multiplier": "10",
            },
        )
        # The addresses are untouched by carrying rates alongside them.
        self.assertEqual(parsed.uri[0].port, 8090)
        self.assertEqual(list(parsed.uri[0].transport.tags), ["tcp"])

    def test_rates_reach_the_reputation_json(self):
        """submit_to_ledger republishes the stored Peer and JSON-encodes it."""
        parsed = celaut.Peer()
        parsed.ParseFromString(self._peer_with_rates().SerializeToString())

        published = MessageToJson(parsed)

        self.assertIn("ram_mu_per_gib_second", published)
        self.assertIn("net_mu_per_gib", published)


if __name__ == "__main__":
    unittest.main()

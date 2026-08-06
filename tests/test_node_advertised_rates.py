"""Tests for the recurring rates a node advertises to its peers.

The rates ride inside ``Service.Api.Slot.gas_amount_per_call`` of the gateway slot,
which is exactly the slot a receiving peer stores verbatim in
``peer.protocol_stack`` (``manager.add_peer_instance``) and that ``submit_to_ledger``
reconstructs for the reputation JSON. So the test that matters is the round trip:
the rates have to survive being serialized and parsed back the way those two paths
do it, or the whole mechanism is decoration.
"""

import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from google.protobuf.json_format import MessageToJson

    from protos import celaut_pb2 as celaut
    from src.utils.cost_functions import general_cost_functions as rates_module
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    rates_module = None  # type: ignore[assignment]


def _config(**overrides):
    """Override only the rate keys; ConfigManager is a shared singleton."""
    values = {
        "EXECUTION_COST": 100.0,
        "MANAGER_ITERATION_TIME": 10,
        "costs.TUNNEL_OPEN_COST": 10.0,
        "costs.TUNNEL_COST_PER_KB": 1.0,
    }
    values.update(overrides)
    real_get = rates_module.env_manager.get

    def get(key, default=None):
        return values[key] if key in values else real_get(key, default)

    return patch.object(rates_module.env_manager, "get", side_effect=get)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NodeAdvertisedRatesTests(unittest.TestCase):
    def test_the_three_recurring_rates_are_advertised(self):
        with _config():
            advertised = rates_module.node_advertised_rates()

        self.assertEqual(
            advertised,
            {
                # 100 gas ceiling charged once per 10s manager tick -> 10 gas/s.
                "maintenance_max_per_second": 10,
                "tunnel_open": 10,
                "tunnel_per_kb": 1,
            },
        )

    def test_maintenance_is_normalised_by_the_manager_cadence(self):
        """A slower manager loop means a lower per-second ceiling, comparably."""
        with _config(**{"MANAGER_ITERATION_TIME": 50}):
            advertised = rates_module.node_advertised_rates()

        self.assertEqual(advertised["maintenance_max_per_second"], 2)

    def test_a_zero_rate_is_omitted_rather_than_advertised_as_free(self):
        with _config(**{"costs.TUNNEL_OPEN_COST": 0}):
            advertised = rates_module.node_advertised_rates()

        self.assertNotIn("tunnel_open", advertised)
        self.assertIn("tunnel_per_kb", advertised)

    def test_a_malformed_rate_is_omitted_not_crashing(self):
        with _config(**{"costs.TUNNEL_COST_PER_KB": "cheap"}):
            advertised = rates_module.node_advertised_rates()

        self.assertNotIn("tunnel_per_kb", advertised)

    def test_a_missing_cadence_drops_only_the_maintenance_rate(self):
        with _config(**{"MANAGER_ITERATION_TIME": 0}):
            advertised = rates_module.node_advertised_rates()

        self.assertNotIn("maintenance_max_per_second", advertised)
        self.assertIn("tunnel_open", advertised)

    def test_rates_below_one_gas_are_omitted(self):
        """Integer gas cannot express a fraction; claiming 0 would read as free."""
        with _config(**{"EXECUTION_COST": 1.0, "MANAGER_ITERATION_TIME": 100}):
            advertised = rates_module.node_advertised_rates()

        self.assertNotIn("maintenance_max_per_second", advertised)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AdvertisedRatesSurviveTheWireTests(unittest.TestCase):
    """The rates must survive the two paths a peer's slot actually goes through."""

    def _slot_with_rates(self) -> "celaut.Service.Api.Slot":
        slot = celaut.Service.Api.Slot(
            port=8090,
            transport=celaut.Service.Api.Protocol(tags=["tcp"]),
        )
        with _config():
            for rate, gas in rates_module.node_advertised_rates().items():
                slot.gas_amount_per_call[rate].n = str(gas)
        return slot

    def test_rates_survive_the_peer_protocol_stack_round_trip(self):
        """add_peer_instance stores slot bytes; peers.py parses them back."""
        stored = self._slot_with_rates().SerializeToString()

        parsed = celaut.Service.Api.Slot()
        parsed.ParseFromString(stored)

        self.assertEqual(
            {rate: gas.n for rate, gas in parsed.gas_amount_per_call.items()},
            {
                "maintenance_max_per_second": "10",
                "tunnel_open": "10",
                "tunnel_per_kb": "1",
            },
        )
        # The rest of the slot is untouched by carrying rates.
        self.assertEqual(parsed.port, 8090)
        self.assertEqual(list(parsed.transport.tags), ["tcp"])

    def test_rates_reach_the_reputation_json(self):
        """submit_to_ledger rebuilds the slot into an Instance and JSON-encodes it."""
        parsed = celaut.Service.Api.Slot()
        parsed.ParseFromString(self._slot_with_rates().SerializeToString())

        instance = celaut.Instance()
        instance.api.slot.append(parsed)
        published = MessageToJson(instance)

        self.assertIn("maintenance_max_per_second", published)
        self.assertIn("tunnel_per_kb", published)

    def test_a_slot_from_an_older_peer_has_no_rates_and_that_is_fine(self):
        """Peers predating this feature must not break the reader."""
        old = celaut.Service.Api.Slot(
            port=8090, transport=celaut.Service.Api.Protocol(tags=["tcp"])
        )

        parsed = celaut.Service.Api.Slot()
        parsed.ParseFromString(old.SerializeToString())

        self.assertEqual(dict(parsed.gas_amount_per_call), {})


if __name__ == "__main__":
    unittest.main()

"""Conversion between local MU scales must be explicit and never round our way."""
import unittest
from unittest.mock import Mock, patch

from protos import celaut_pb2
from src.payment_system.mu_conversion import (
    MatchingPaymentSystem,
    configuration_for_peer,
    convert_mu,
    estimated_cost_for_local,
    matching_payment_system,
)


class MuConversionTests(unittest.TestCase):
    def test_selects_the_single_common_contract_and_converts_in_both_directions(self):
        connection = Mock()
        connection.get_peer_payment_contracts.return_value = [
            {"ledger_tag": "ergo", "contract_hash": "p2pk", "mu_per_unit": 2_000_000_000}
        ]

        # Our own rate comes from what we advertise, never from the LOCAL row:
        # nothing ever writes a rate into it (see `_local_rates`).
        with patch(
            "src.payment_system.mu_conversion._local_rates",
            return_value={("ergo", "p2pk"): 1_000_000_000},
        ):
            payment_system = matching_payment_system("peer-a", connection=connection)

        self.assertEqual(payment_system.local_mu_per_unit, 1_000_000_000)
        self.assertEqual(payment_system.peer_mu_per_unit, 2_000_000_000)
        self.assertEqual(
            convert_mu(1_000_000, from_mu_per_unit=1_000_000_000, to_mu_per_unit=2_000_000_000),
            2_000_000,
        )
        self.assertEqual(
            convert_mu(2_000_000, from_mu_per_unit=2_000_000_000, to_mu_per_unit=1_000_000_000),
            1_000_000,
        )

    def test_converts_configuration_and_quote_without_mutating_local_values(self):
        payment_system = MatchingPaymentSystem(
            ledger_tag="ergo",
            contract_hash="p2pk",
            local_mu_per_unit=1_000_000_000,
            peer_mu_per_unit=2_000_000_000,
        )
        config = celaut_pb2.Configuration()
        config.initial_mu.n = "1000000"
        peer_config = configuration_for_peer(config, payment_system=payment_system)

        peer_quote = celaut_pb2.EstimatedCost()
        peer_quote.cost.n = "2000000"
        peer_quote.init_maintenance_cost.n = "400"
        peer_quote.max_maintenance_cost.n = "600"
        local_quote = estimated_cost_for_local(peer_quote, payment_system=payment_system)

        self.assertEqual(config.initial_mu.n, "1000000")
        self.assertEqual(peer_config.initial_mu.n, "2000000")
        self.assertEqual(local_quote.cost.n, "1000000")
        self.assertEqual(local_quote.init_maintenance_cost.n, "200")
        self.assertEqual(local_quote.max_maintenance_cost.n, "300")

    def test_refuses_an_ambiguous_payment_system(self):
        connection = Mock()
        contracts = [
            {"ledger_tag": "ergo", "contract_hash": "a", "mu_per_unit": 1},
            {"ledger_tag": "other", "contract_hash": "b", "mu_per_unit": 2},
        ]
        with patch(
            "src.payment_system.mu_conversion._local_rates",
            return_value={("ergo", "a"): 1, ("other", "b"): 2},
        ):
            connection.get_peer_payment_contracts.return_value = contracts
            with self.assertRaisesRegex(ValueError, "multiple common payment systems"):
                matching_payment_system("peer-a", connection=connection)

    def test_rounding_never_goes_in_our_favour(self):
        # 2 MU of ours is worth 1.33... of theirs. What we hand them rounds down
        # (never promise value the payment does not carry); what we take back
        # rounds up (never charge our own client less than we owe).
        self.assertEqual(convert_mu(2, from_mu_per_unit=3, to_mu_per_unit=2), 1)
        self.assertEqual(
            convert_mu(2, from_mu_per_unit=3, to_mu_per_unit=2, round_up=True), 2
        )


if __name__ == "__main__":
    unittest.main()

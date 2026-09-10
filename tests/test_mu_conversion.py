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
    matching_payment_systems,
)


class MuConversionTests(unittest.TestCase):
    def test_selects_the_single_common_contract_and_converts_in_both_directions(self):
        connection = Mock()
        connection.get_peer_payment_contracts.return_value = [
            {"ledger_tag": "ergo", "contract_hash": "p2pk", "token_id": "ERG",
             "mu_per_unit": 2_000_000_000}
        ]

        # Our own rate comes from what we advertise, never from the LOCAL row:
        # nothing ever writes a rate into it (see `_local_rates`).
        with patch(
            "src.payment_system.mu_conversion._local_rates",
            return_value={("ergo", "p2pk", "ERG"): 1_000_000_000},
        ):
            payment_system = matching_payment_system("peer-a", connection=connection)

        self.assertEqual(payment_system.local_mu_per_unit, 1_000_000_000)
        self.assertEqual(payment_system.peer_mu_per_unit, 2_000_000_000)
        # A payment method is ledger + contract + asset, and the asset is what the
        # dispatch is keyed by.
        self.assertEqual(payment_system.asset, "ERG")
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

    def _two_shared(self):
        connection = Mock()
        connection.get_peer_payment_contracts.return_value = [
            {"ledger_tag": "ergo", "contract_hash": "a", "token_id": "ERG", "mu_per_unit": 1},
            {"ledger_tag": "bitcoin", "contract_hash": "b", "token_id": "BTC", "mu_per_unit": 2},
        ]
        return connection

    def test_two_assets_on_one_contract_are_two_payment_systems(self):
        """The case the asset dimension exists for.

        On Ergo one P2PK contract is paid in ERG and in every EIP-4 token at the same
        address: same script, same address, same contract_hash. Keyed without the asset
        these two rows read as one contract advertised twice at two different rates --
        which `_rates_by_payment_system` refuses as a contradiction, so the peer would
        become unpayable rather than payable in two currencies.
        """
        token = "ab" * 32
        connection = Mock()
        connection.get_peer_payment_contracts.return_value = [
            {"ledger_tag": "ergo", "contract_hash": "p2pk", "token_id": "ERG",
             "mu_per_unit": 1_000_000_000},
            {"ledger_tag": "ergo", "contract_hash": "p2pk", "token_id": token,
             "mu_per_unit": 20_000_000},
        ]
        with patch(
            "src.payment_system.mu_conversion._local_rates",
            return_value={
                ("ergo", "p2pk", "ERG"): 1_000_000_000,
                ("ergo", "p2pk", token): 20_000_000,
            },
        ):
            systems = matching_payment_systems("peer-a", connection=connection)

        self.assertEqual([s.asset for s in systems], ["ERG", token])
        self.assertEqual([s.contract_hash for s in systems], ["p2pk", "p2pk"])
        # Each carries its own rate, which is the whole point: one row for both would
        # have every peer converting ERG amounts at the token's rate.
        self.assertEqual([s.local_mu_per_unit for s in systems], [1_000_000_000, 20_000_000])

    def test_the_same_method_advertised_twice_at_two_rates_is_still_refused(self):
        # A genuine contradiction, as opposed to two assets: same ledger, same
        # contract, same asset, two rates.
        connection = Mock()
        connection.get_peer_payment_contracts.return_value = [
            {"ledger_tag": "ergo", "contract_hash": "p2pk", "token_id": "ERG",
             "mu_per_unit": 1},
            {"ledger_tag": "ergo", "contract_hash": "p2pk", "token_id": "ERG",
             "mu_per_unit": 2},
        ]
        with patch(
            "src.payment_system.mu_conversion._local_rates",
            return_value={("ergo", "p2pk", "ERG"): 1},
        ):
            with self.assertRaisesRegex(ValueError, "conflicting MU rates"):
                matching_payment_systems("peer-a", connection=connection)

    def test_two_shared_systems_are_both_offered(self):
        """Sharing two currencies used to mean being unable to pay at all.

        `matching_payment_system` raised "payment selection is not implemented" on more
        than one match, so two nodes that both accepted ERG and BTC were worse off than
        two that accepted one each. Funding is the selection: the payer walks this list
        and settles through the first system it can fund.
        """
        connection = self._two_shared()
        with patch(
            "src.payment_system.mu_conversion._local_rates",
            # Insertion order is the registry's candidate order, which is the payer's
            # preference -- not alphabetical, and not whatever the hashes sort to.
            return_value={("bitcoin", "b", "BTC"): 20, ("ergo", "a", "ERG"): 10},
        ):
            systems = matching_payment_systems("peer-a", connection=connection)

        self.assertEqual(
            [(s.ledger_tag, s.contract_hash) for s in systems],
            [("bitcoin", "b"), ("ergo", "a")],
        )
        # Each carries the pair of rates for its own system, not another's.
        self.assertEqual((systems[0].local_mu_per_unit, systems[0].peer_mu_per_unit), (20, 2))
        self.assertEqual((systems[1].local_mu_per_unit, systems[1].peer_mu_per_unit), (10, 1))

    def test_the_singular_form_takes_the_first_for_quoting(self):
        # For the callers that need a rate rather than a settlement, and so cannot try
        # the next one: a balance held on a peer, a cost it metered, a quote.
        connection = self._two_shared()
        with patch(
            "src.payment_system.mu_conversion._local_rates",
            return_value={("bitcoin", "b", "BTC"): 20, ("ergo", "a", "ERG"): 10},
        ):
            system = matching_payment_system("peer-a", connection=connection)
        self.assertEqual(system.ledger_tag, "bitcoin")

    def test_no_shared_system_at_all_still_raises(self):
        # Not a retry case: no amount of trying finds a currency two nodes do not both
        # accept.
        connection = Mock()
        connection.get_peer_payment_contracts.return_value = [
            {"ledger_tag": "bitcoin", "contract_hash": "b", "token_id": "BTC", "mu_per_unit": 2},
        ]
        with patch(
            "src.payment_system.mu_conversion._local_rates",
            return_value={("ergo", "a", "ERG"): 1},
        ):
            with self.assertRaisesRegex(ValueError, "no common payment system"):
                matching_payment_systems("peer-a", connection=connection)

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

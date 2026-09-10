"""A deposit crosses to a peer, so the figure it carries is the *peer's* MU.

MU is an internal unit. We move real value on a ledger at our own MU/unit rate,
and the peer credits its client at its own -- so the number on the wire has to be
translated, or every deposit is mis-credited by the ratio between the two scales.
The ledger's minimum output is a floor on the value actually moved, so it applies
to our figure first and the peer's figure follows from what we end up sending.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.payment_system import payment_process
    from src.payment_system.mu_conversion import MatchingPaymentSystem
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    payment_process = None  # type: ignore[assignment]

# Fetched by name: `payment_process.__deposit_amounts` written inside a class body
# would be mangled to `_TestClass__deposit_amounts`.
settlement_plans = getattr(payment_process, "__settlement_plans", None)

ERGO = "1c691f72"


def _system(local: int, peer: int, *, contract=None, ledger="ergo") -> "MatchingPaymentSystem":
    return MatchingPaymentSystem(
        ledger_tag=ledger, contract_hash=contract or ERGO,
        local_mu_per_unit=local, peer_mu_per_unit=peer,
    )


def _envs(floors_by_contract, demos=()):
    """A payment-envs registry with the floors each contract reports."""
    return type("envs", (), {
        "DEMOS": tuple(demos),
        "settlement_floors": staticmethod(
            lambda: {
                name: (lambda pair=pair: pair)
                for name, pair in floors_by_contract.items()
            }
        ),
    })


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SettlementPlanTests(unittest.TestCase):
    """One plan per shared payment system, each with *its own* figures.

    Both figures used to be resolved once, from whichever system happened to be picked,
    and then handed to a loop free to settle through a different one. With one contract
    they cannot disagree; with two they can, silently -- the node pays over one chain
    and tells the peer a figure converted at another's rate, so the peer's validator
    rejects a payment that is already on-chain (#340 §4.4).
    """

    def _plans(self, amount, systems, floors_by_contract, *, floor=False, demos=(),
               contract_hash=None):
        envs = _envs(floors_by_contract, demos)
        with patch.object(payment_process, "format_mu", str), patch(
            "src.payment_system.mu_conversion.matching_payment_systems",
            return_value=list(systems),
        ), patch.object(
            payment_process, "_payment_envs", return_value=envs,
        ), patch(
            # `deposits` reads the real dispatch rather than the payer's handle on it,
            # so sizing a full deposit goes through here.
            "src.payment_system.contracts.envs.settlement_floors",
            envs.settlement_floors,
        ):
            return settlement_plans(
                peer_id="peer-a", amount=amount, floor=floor,
                contract_hash=contract_hash,
            )

    def test_the_peer_is_told_its_own_mu(self):
        # One ERG buys twice as many MU on the peer, so our 1_000_000 MU of value
        # is 2_000_000 of theirs. Sending our own figure would halve the credit.
        plans, refusals = self._plans(
            1_000_000, [_system(1_000_000_000, 2_000_000_000)], {ERGO: (0, 0)}
        )
        self.assertEqual(refusals, [])
        self.assertEqual([(p.amount, p.peer_amount) for p in plans], [(1_000_000, 2_000_000)])

    def test_the_peers_figure_rounds_down(self):
        # 2 of our MU is worth 1.33... of theirs. Claiming 2 would ask the peer to
        # credit more value than the transaction carries, and its validator -- which
        # checks the box holds *at least* what is claimed -- would reject a payment
        # already on-chain.
        plans, _ = self._plans(2, [_system(3, 2)], {ERGO: (0, 0)})
        self.assertEqual(plans[0].peer_amount, 1)

    def test_each_system_converts_at_its_own_rate(self):
        """The regression this restructure exists for.

        Two candidates, two rates. A single figure derived from the first and used for
        whichever settled would credit the peer an amount its own validator never saw.
        """
        plans, _ = self._plans(
            1_000_000,
            [
                _system(1_000_000_000, 2_000_000_000, contract="ergo-c", ledger="ergo"),
                _system(1_000_000_000, 500_000_000, contract="btc-c", ledger="bitcoin"),
            ],
            {"ergo-c": (0, 0), "btc-c": (0, 0)},
        )
        self.assertEqual(
            [(p.ledger_tag, p.peer_amount) for p in plans],
            [("ergo", 2_000_000), ("bitcoin", 500_000)],
        )

    def test_each_system_applies_its_own_floors(self):
        """A figure below one chain's dust limit is still payable on another.

        The floors used to be one maximum across every contract, so the cheapest system
        inherited the most expensive one's minimum output and a perfectly payable Ergo
        deposit was refused because Bitcoin could not have carried it (#340 §5).
        """
        plans, refusals = self._plans(
            1_000_000,
            [
                _system(1, 1, contract="cheap", ledger="ergo"),
                _system(1, 1, contract="costly", ledger="bitcoin"),
            ],
            {"cheap": (0, 1_000), "costly": (0, 100_000_000)},
        )
        self.assertEqual([p.contract_hash for p in plans], ["cheap"])
        self.assertEqual(len(refusals), 1)
        self.assertIn("bitcoin", refusals[0])

    def test_a_named_system_is_the_only_one_planned_for(self):
        """`nodo pay --ledger bitcoin` must settle on Bitcoin.

        The amount was read in that ledger's own unit and checked against that ledger's
        floors and wallet, so a walk free to settle on the next funded system would move
        a figure typed in one currency over another (#340 §4).
        """
        plans, refusals = self._plans(
            1_000_000,
            [
                _system(1_000_000_000, 2_000_000_000, contract="ergo-c", ledger="ergo"),
                _system(1_000_000_000, 500_000_000, contract="btc-c", ledger="bitcoin"),
            ],
            {"ergo-c": (0, 0), "btc-c": (0, 0)},
            contract_hash="btc-c",
        )
        self.assertEqual(refusals, [])
        self.assertEqual([(p.contract_hash, p.ledger_tag) for p in plans],
                         [("btc-c", "bitcoin")])

    def test_a_named_system_the_peer_does_not_share_is_refused_not_replaced(self):
        # Falling back to the other chain is the failure this guards: the operator would
        # be told the payment succeeded, having named a chain it never touched.
        plans, refusals = self._plans(
            1_000_000,
            [_system(1_000_000_000, 2_000_000_000, contract="ergo-c", ledger="ergo")],
            {"ergo-c": (0, 0)},
            contract_hash="btc-c",
        )
        self.assertEqual(plans, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn("btc-c", refusals[0])
        self.assertIn("ergo", refusals[0])

    def test_naming_a_system_still_applies_that_systems_floors(self):
        # Narrowing must not become a way past the floor check: below the named chain's
        # minimum output there is no transaction to build, and the other chain's ability
        # to carry the figure is irrelevant once a chain has been named.
        plans, refusals = self._plans(
            1_000,
            [
                _system(1, 1, contract="cheap", ledger="ergo"),
                _system(1, 1, contract="costly", ledger="bitcoin"),
            ],
            {"cheap": (0, 1), "costly": (0, 100_000_000)},
            contract_hash="costly",
        )
        self.assertEqual(plans, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn("bitcoin", refusals[0])

    def test_an_automatic_refill_is_raised_to_what_that_system_can_settle(self):
        # The automatic refill named no figure, so it is raised -- to a full deposit for
        # *this* system, and what the peer is told is the converted result rather than
        # the amount originally asked for.
        plans, _ = self._plans(
            10, [_system(1_000_000_000, 2_000_000_000)],
            {ERGO: (100_000, 1_000_000)}, floor=True,
        )
        # fee/MAX_FEE_OVERHEAD (0.02) = 5_000_000, above the 1_100_000 settleable floor.
        self.assertEqual((plans[0].amount, plans[0].peer_amount), (5_000_000, 10_000_000))

    def test_an_operator_figure_below_the_ledger_floor_is_refused(self):
        # An operator who typed an amount gets that amount or a reason, never a larger
        # payment they did not ask for. Refused before a deposit token is issued or the
        # wallet is touched.
        plans, refusals = self._plans(
            10, [_system(1_000_000_000, 2_000_000_000)], {ERGO: (100_000, 1_000_000)}
        )
        self.assertEqual(plans, [])
        self.assertIn("smallest output it can create", refusals[0])

    def test_refuses_a_deposit_worth_less_than_one_of_the_peers_mu(self):
        # Rounding down to zero would broadcast a transaction buying no credit.
        plans, refusals = self._plans(1, [_system(1_000_000_000, 1)], {ERGO: (0, 0)})
        self.assertEqual(plans, [])
        self.assertIn("less than a single one of the peer", refusals[0])

    def test_a_simulated_payment_has_no_scale_to_convert_through(self):
        with patch(
            "src.payment_system.mu_conversion.matching_payment_systems",
            side_effect=ValueError("no common payment system"),
        ), patch.object(
            payment_process, "_payment_envs", return_value=_envs({}, demos=("demo",))
        ):
            plans, refusals = settlement_plans(peer_id="peer-a", amount=7, floor=False)
        self.assertEqual([(p.amount, p.peer_amount, p.is_demo) for p in plans],
                         [(7, 7, True)])
        self.assertEqual(refusals, [])

    def test_a_real_payment_stops_when_no_payment_system_is_shared(self):
        # Not a "try the next one" case: no amount of retrying finds a currency two
        # nodes do not both accept.
        with patch(
            "src.payment_system.mu_conversion.matching_payment_systems",
            side_effect=ValueError("no common payment system"),
        ), patch.object(payment_process, "_payment_envs", return_value=_envs({})):
            with self.assertRaises(ValueError):
                settlement_plans(peer_id="peer-a", amount=7, floor=False)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DepositRefusalReasonTests(unittest.TestCase):
    """The operator gets the reason on screen, not a bare "it failed".

    `nodo pay` and `nodo increase_peer_deposit` ask this before touching the
    wallet, so a refusal reads as a clean stop with nothing broadcast.
    """

    def _reason(self, amount, systems, floors_by_contract):
        with patch.object(payment_process, "format_mu", str), patch(
            "src.payment_system.mu_conversion.matching_payment_systems",
            return_value=list(systems),
        ), patch.object(
            payment_process, "_payment_envs", return_value=_envs(floors_by_contract)
        ):
            return payment_process.deposit_refusal_reason("peer-a", amount)

    def test_none_when_the_deposit_can_be_settled(self):
        self.assertIsNone(self._reason(
            1_000_000, [_system(1_000_000_000, 2_000_000_000)], {ERGO: (0, 1_000)}
        ))

    def test_none_when_any_shared_system_can_settle_it(self):
        # A figure below one chain's dust limit is not a refusal when the same peer
        # also accepts a cheaper one.
        self.assertIsNone(self._reason(
            1_000,
            [
                _system(1, 1, contract="costly", ledger="bitcoin"),
                _system(1, 1, contract="cheap", ledger="ergo"),
            ],
            {"costly": (0, 100_000_000), "cheap": (0, 100)},
        ))

    def test_says_how_small_the_amount_is_and_against_what(self):
        reason = self._reason(
            10, [_system(1_000_000_000, 2_000_000_000)], {ERGO: (0, 1_000_000)}
        )
        self.assertIn("10", reason)
        self.assertIn("1000000", reason)
        self.assertIn("nothing was broadcast", reason)

    def test_every_floor_is_reported_not_just_the_first(self):
        # An operator choosing a new amount needs to know which floor to clear.
        reason = self._reason(
            10,
            [
                _system(1, 1, contract="ergo-c", ledger="ergo"),
                _system(1, 1, contract="btc-c", ledger="bitcoin"),
            ],
            {"ergo-c": (0, 1_000), "btc-c": (0, 100_000_000)},
        )
        self.assertIn("ergo", reason)
        self.assertIn("bitcoin", reason)

    def test_reports_a_peer_we_share_no_payment_system_with(self):
        with patch(
            "src.payment_system.mu_conversion.matching_payment_systems",
            side_effect=ValueError("no common payment system is registered"),
        ), patch.object(payment_process, "_payment_envs", return_value=_envs({})):
            self.assertIn(
                "no common payment system",
                payment_process.deposit_refusal_reason("peer-a", 1_000_000),
            )


if __name__ == "__main__":
    unittest.main()

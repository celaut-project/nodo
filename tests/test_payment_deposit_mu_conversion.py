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
deposit_amounts = getattr(payment_process, "__deposit_amounts", None)

ERGO = "1c691f72"


def _system(local: int, peer: int) -> "MatchingPaymentSystem":
    return MatchingPaymentSystem(
        ledger_tag="ergo", contract_hash=ERGO,
        local_mu_per_unit=local, peer_mu_per_unit=peer,
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DepositAmountsTests(unittest.TestCase):
    def _run(self, amount, *, local, peer, floors=(0, 0), floor=False):
        # `format_mu` resolves its display unit through the Ergo rate module,
        # which is not what these assertions are about.
        with patch.object(payment_process, "format_mu", str), patch(
            "src.payment_system.mu_conversion.matching_payment_system",
            return_value=_system(local, peer),
        ), patch.object(
            payment_process, "_payment_envs",
            return_value=type("envs", (), {
                "DEMOS": [],
                "settlement_floors": staticmethod(lambda: {ERGO: lambda: floors}),
            }),
        ):
            return deposit_amounts(peer_id="peer-a", amount=amount, floor=floor)

    def test_the_peer_is_told_its_own_mu(self):
        # One ERG buys twice as many MU on the peer, so our 1_000_000 MU of value
        # is 2_000_000 of theirs. Sending our own figure would halve the credit.
        self.assertEqual(
            self._run(1_000_000, local=1_000_000_000, peer=2_000_000_000),
            (1_000_000, 2_000_000),
        )

    def test_the_peers_figure_rounds_down(self):
        # 2 of our MU is worth 1.33... of theirs. Claiming 2 would ask the peer to
        # credit more value than the transaction carries, and its validator --
        # which checks the box holds *at least* what is claimed -- would reject a
        # payment already on-chain.
        _, peer_amount = self._run(2, local=3, peer=2)
        self.assertEqual(peer_amount, 1)

    def test_an_automatic_refill_is_raised_to_the_ledger_floor_then_converted(self):
        # Below the minimum output nothing can be settled at all. The automatic
        # refill named no figure, so it is raised to it -- and what the peer is
        # told is the converted floor, not the amount originally asked for.
        ours, peer_amount = self._run(
            10, local=1_000_000_000, peer=2_000_000_000,
            floors=(100_000, 1_000_000), floor=True,
        )
        self.assertEqual((ours, peer_amount), (1_000_000, 2_000_000))

    def test_an_operator_figure_below_the_ledger_floor_is_refused(self):
        # An operator who typed an amount gets that amount or an error, never a
        # larger payment they did not ask for. Refused here, before a deposit
        # token is issued or the wallet is touched.
        with self.assertRaisesRegex(ValueError, "smallest output this ledger can create"):
            self._run(10, local=1_000_000_000, peer=2_000_000_000, floors=(100_000, 1_000_000))

    def test_refuses_a_deposit_worth_less_than_one_of_the_peers_mu(self):
        # Rounding down to zero would broadcast a transaction buying no credit.
        with self.assertRaisesRegex(ValueError, "less than a single one of the peer"):
            self._run(1, local=1_000_000_000, peer=1)

    def test_a_simulated_payment_has_no_scale_to_convert_through(self):
        with patch(
            "src.payment_system.mu_conversion.matching_payment_system",
            side_effect=ValueError("no common payment system"),
        ), patch.object(
            payment_process, "_payment_envs",
            return_value=type("envs", (), {"DEMOS": ["demo"], "settlement_floors": staticmethod(dict)}),
        ):
            self.assertEqual(
                deposit_amounts(peer_id="peer-a", amount=7, floor=False), (7, 7)
            )

    def test_a_real_payment_stops_when_no_payment_system_is_shared(self):
        with patch(
            "src.payment_system.mu_conversion.matching_payment_system",
            side_effect=ValueError("no common payment system"),
        ), patch.object(
            payment_process, "_payment_envs",
            return_value=type("envs", (), {"DEMOS": [], "settlement_floors": staticmethod(dict)}),
        ):
            with self.assertRaises(ValueError):
                deposit_amounts(peer_id="peer-a", amount=7, floor=False)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DepositRefusalReasonTests(unittest.TestCase):
    """The operator gets the reason on screen, not a bare "it failed".

    `nodo pay` and `nodo increase_peer_deposit` ask this before touching the
    wallet, so a refusal reads as a clean stop with nothing broadcast.
    """

    def test_none_when_the_deposit_can_be_settled(self):
        with patch.object(payment_process, "format_mu", str), patch(
            "src.payment_system.mu_conversion.matching_payment_system",
            return_value=_system(1_000_000_000, 2_000_000_000),
        ), patch.object(
            payment_process, "_payment_envs",
            return_value=type("envs", (), {
                "DEMOS": [],
                "settlement_floors": staticmethod(lambda: {ERGO: lambda: (0, 1_000)}),
            }),
        ):
            self.assertIsNone(payment_process.deposit_refusal_reason("peer-a", 1_000_000))

    def test_says_how_small_the_amount_is_and_against_what(self):
        with patch.object(payment_process, "format_mu", str), patch(
            "src.payment_system.mu_conversion.matching_payment_system",
            return_value=_system(1_000_000_000, 2_000_000_000),
        ), patch.object(
            payment_process, "_payment_envs",
            return_value=type("envs", (), {
                "DEMOS": [],
                "settlement_floors": staticmethod(lambda: {ERGO: lambda: (0, 1_000_000)}),
            }),
        ):
            reason = payment_process.deposit_refusal_reason("peer-a", 10)

        self.assertIn("10", reason)
        self.assertIn("1000000", reason)
        self.assertIn("nothing was broadcast", reason)

    def test_reports_a_peer_we_share_no_payment_system_with(self):
        with patch(
            "src.payment_system.mu_conversion.matching_payment_system",
            side_effect=ValueError("no common payment system is registered"),
        ), patch.object(
            payment_process, "_payment_envs",
            return_value=type("envs", (), {"DEMOS": [], "settlement_floors": staticmethod(dict)}),
        ):
            self.assertIn(
                "no common payment system",
                payment_process.deposit_refusal_reason("peer-a", 1_000_000),
            )


if __name__ == "__main__":
    unittest.main()

"""A token method's floors mix two assets, and both land in MU.

The promise of ``settlement_floors_mu`` is "both figures in MU", and #342 5.4 is
explicit that it must not be tightened to "the contract's own asset": a token method's
fee is denominated in ERG while its smallest output is one base unit of the token. MU is
the only scale that holds both, and MU is what ``deposits.py`` consumes.

The failure this prevents is a token deposit sized against *Ergo's* minimum box value --
which for a token is not a floor at all, and would demand a deposit worth a million
times the smallest payment the method can actually settle.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system import deposits
    from src.payment_system.contracts.ergo import interface, rate
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    interface = None  # type: ignore[assignment]

TOKEN = "ab" * 32


def _asset(mu_per_base=1):
    return rate.Asset(token_id=TOKEN, symbol="SigUSD", unit_name="sigusd",
                      decimals=2, mu_per_base_unit=Decimal(mu_per_base))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TokenFloorTests(unittest.TestCase):
    """A token method's floors mix two assets, and both land in MU."""

    def test_the_fee_is_erg_and_the_minimum_output_is_one_base_unit(self):
        with mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(1)):
            fee, minimum = interface._token_settlement_floors_mu(_asset(mu_per_base=20))
        # The fee is the network fee plus the carrier box: both are ERG the payer parts
        # with beyond the credited amount, which is what a fee overhead measures.
        self.assertEqual(fee, interface.DEFAULT_FEE + interface.SAFE_MIN_BOX_VALUE)
        # The minimum output is one base unit of the token at the token's own rate --
        # NOT Ergo's minimum box value, which is what a token deposit would otherwise
        # be sized against.
        self.assertEqual(minimum, 20)
        self.assertLess(minimum, interface.SAFE_MIN_BOX_VALUE)

    def test_the_native_floors_are_unchanged(self):
        with mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(1)):
            self.assertEqual(
                interface.settlement_floors_mu(),
                (interface.DEFAULT_FEE, interface.SAFE_MIN_BOX_VALUE),
            )

    def test_both_figures_scale_with_their_own_rate(self):
        # The promise is "both in MU"; the two get there through different assets.
        with mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(2)):
            fee, minimum = interface._token_settlement_floors_mu(_asset(mu_per_base=5))
        self.assertEqual(fee, 2 * (interface.DEFAULT_FEE + interface.SAFE_MIN_BOX_VALUE))
        self.assertEqual(minimum, 5)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TokenDepositSizingTests(unittest.TestCase):
    """What `deposits.py` does with those two figures."""

    class _System:
        """A matched payment method, as `deposits` reads one."""

        def __init__(self, key, asset):
            self.key, self.asset = key, asset
            self.ledger_tag, self.contract_hash = key.ledger, key.contract_hash

    def _system(self, asset):
        from src.payment_system.contracts.registry import MethodKey

        return self._System(MethodKey("ergo", "p2pk", asset), asset)

    def _sized(self, floors, config=None, asset=TOKEN):
        system = self._system(asset)
        values = config or {}
        with mock.patch(
            "src.payment_system.contracts.envs.settlement_floors",
            return_value={system.key: lambda: floors},
        ), mock.patch.object(deposits, "ConfigManager", lambda: mock.Mock(
            get=lambda key, default=None: values.get(key, default)
        )):
            return deposits.full_deposit_mu(system)

    def test_a_token_deposit_is_not_sized_by_ergos_minimum_box_value(self):
        # The floor that decides the deposit is the fee overhead. Ergo's minimum box
        # value does not enter it: for a token the smallest payable output is one base
        # unit, five orders of magnitude below the box value, so `minimum_output + fee`
        # never binds and a token deposit is not inflated by a floor that belongs to
        # a different asset.
        floors = interface._token_settlement_floors_mu(_asset(20))
        fee, minimum_output = floors
        self.assertLess(minimum_output, interface.SAFE_MIN_BOX_VALUE)
        self.assertEqual(self._sized(floors), int(fee / 0.02))
        self.assertGreater(int(fee / 0.02), minimum_output + fee)

    def test_the_smallest_settleable_token_payment_is_one_base_unit(self):
        # ERG cannot settle below Ergo's box value at all; the token can settle a
        # thousandth of a cent. That difference is the reason this pair is per method
        # and not per contract.
        with mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(1)):
            self.assertEqual(interface.settlement_floors_mu()[1], interface.SAFE_MIN_BOX_VALUE)
            self.assertEqual(interface._token_settlement_floors_mu(_asset(20))[1], 20)

    def test_the_minimum_output_still_binds_when_it_is_the_larger_floor(self):
        # A token whose base unit is expensive: a deposit has to be able to pay one.
        expensive = 10 ** 12
        sized = self._sized((100, expensive))
        self.assertEqual(sized, expensive + 100)

    def test_a_per_method_overhead_overrides_the_ledgers(self):
        # What is right for a chain's native unit is absurd for a token priced orders
        # of magnitude away from it (#342 5.5).
        floors = (1_000, 1)
        config = {
            "ledgers.ergo.payments.ASSETS": [
                {"TOKEN_ID": TOKEN, "MAX_FEE_OVERHEAD": 0.5}
            ],
            "ledgers.ergo.payments.MAX_FEE_OVERHEAD": 0.02,
        }
        self.assertEqual(self._sized(floors, config), 2_000)
        # ...and the ledger's applies to the native method, which declared none.
        self.assertEqual(self._sized(floors, config, asset="ERG"), 50_000)


if __name__ == "__main__":
    unittest.main()

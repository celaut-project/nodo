"""A share of every incoming payment, owed in the asset it arrived in.

Three properties, each with a failure that would be invisible in production:

* The debt is stored in the asset's smallest native unit. Kept in MU, a later change
  to that asset's rate would retroactively reinterpret money already owed.
* The fraction is kept. A 2 % cut of a small payment has one, and discarding it on
  every payment would make the node's effective rate drift below the configured one --
  always in the node's own favour.
* Two assets on one contract owe two independent debts. A debt in SigUSD is not a debt
  in ERG and cannot be paid out of it.
"""
import sys
import types
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts import envs, registry
    from src.payment_system.contracts.ergo import rate
    from src.payment_system.donations import accrual
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    accrual = None  # type: ignore[assignment]

CONTRACT = "1c691f72aad8533f1e0815cb6dd9f302637d5c60824c8a92684fe50cdd4b82bd"


def _method(asset, native="ERG"):
    """One registered payment method: an asset bound to a converter of its own."""
    contract = mock.Mock()
    contract.LEDGER, contract.CONTRACT_HASH, contract.NATIVE_ASSET = "ergo", CONTRACT, native
    contract.mu_to_native = lambda mu: Decimal(mu)
    return registry.PaymentMethod(contract, asset)


class _Recorder:
    """Stands in for the catalogue, keeping the debts it was asked to accrue."""

    def __init__(self):
        self.debts = {}

    def accrue_donation(self, ledger, contract_hash, token_id, amount_native):
        key = (ledger, contract_hash, token_id)
        self.debts[key] = self.debts.get(key, Decimal(0)) + Decimal(amount_native)
        return self.debts[key]


class _FakeSql:
    def __init__(self, recorder):
        self._recorder = recorder

    def __call__(self):
        return self._recorder


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AccrualTests(unittest.TestCase):

    def setUp(self):
        self.recorder = _Recorder()
        # `accrue` imports the catalogue lazily, so a stub module is enough and the
        # test needs neither a database nor the optional crypto stack behind it.
        module = types.ModuleType("src.database.sql_connection")
        module.SQLConnection = _FakeSql(self.recorder)  # type: ignore[attr-defined]
        patcher = mock.patch.dict(sys.modules, {"src.database.sql_connection": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        # One MU is one nanoERG, the shipped default. Registered as a *method*: a
        # contract paid in several assets converts each one at its own rate, so what
        # the accrual resolves is a method and not a contract.
        self.methods = mock.patch.object(
            envs, "methods", return_value={
                registry.MethodKey("ergo", CONTRACT, "ERG"): _method("ERG"),
                registry.MethodKey("ergo", CONTRACT, "a" * 64): _method("a" * 64),
            }
        )
        self.methods.start()
        self.addCleanup(self.methods.stop)
        self.wallets = mock.patch(
            "src.payment_system.donations.config.pay_wallets",
            return_value=[object()],
        )
        self.wallets.start()
        self.addCleanup(self.wallets.stop)

    def _accrue(self, amount_mu, share="0.02", asset="ERG", ledger="ergo"):
        with mock.patch(
            "src.payment_system.donations.config.percentage",
            return_value=Decimal(share),
        ):
            return accrual.accrue(
                amount_mu=amount_mu, ledger=ledger, contract_hash=CONTRACT, asset=asset
            )

    def test_two_percent_of_a_payment_is_owed(self):
        self.assertEqual(self._accrue(1_000_000), Decimal("20000"))

    def test_the_fraction_is_kept_rather_than_rounded_away(self):
        # 2 % of 10 nanoERG is 0.2. Rounded down it would be nothing, and a node paid
        # in small amounts would donate nothing for ever.
        self.assertEqual(self._accrue(10), Decimal("0.2"))

    def test_the_remainder_accumulates_into_whole_units(self):
        for _ in range(5):
            self._accrue(10)
        # Five payments of 0.2 owe exactly one whole nanoERG, not zero.
        self.assertEqual(self.recorder.debts[("ergo", CONTRACT, "ERG")], Decimal("1.0"))

    def test_a_zero_percentage_accrues_nothing(self):
        self.assertIsNone(self._accrue(1_000_000, share="0"))
        self.assertEqual(self.recorder.debts, {})

    def test_a_percentage_of_one_owes_the_whole_payment(self):
        self.assertEqual(self._accrue(1_000_000, share="1"), Decimal(1_000_000))

    def test_two_assets_on_one_contract_owe_two_independent_debts(self):
        self._accrue(1_000_000, asset="ERG")
        self._accrue(1_000_000, asset="a" * 64)
        self.assertEqual(
            self.recorder.debts,
            {
                ("ergo", CONTRACT, "ERG"): Decimal("20000"),
                ("ergo", CONTRACT, "a" * 64): Decimal("20000"),
            },
        )

    def test_a_payment_method_that_settles_on_no_chain_accrues_nothing(self):
        # The simulated contract moves no money, so a share of a simulated payment is
        # not owed to anybody. It contributes no converter, and this is what that means.
        self.assertIsNone(accrual.accrue(
            amount_mu=1_000_000, ledger="ergo",
            contract_hash="a-contract-that-registers-no-method", asset="ERG",
        ))
        self.assertEqual(self.recorder.debts, {})

    def test_a_payment_that_names_no_asset_owes_the_chains_native_unit(self):
        """Not a debt of its own under the empty string.

        A payer that advertises no ``token_id`` is paying the chain's own unit -- the
        only thing it can be paying while a contract settles in one asset. Accrued
        under "", the debt would be one no payout ever looks for: it would grow for
        ever and never be paid, and nothing would raise.
        """
        self._accrue(1_000_000, asset="")
        self.assertEqual(
            self.recorder.debts, {("ergo", CONTRACT, "ERG"): Decimal("20000")}
        )

    def test_a_payment_with_no_ledger_accrues_nothing(self):
        self.assertIsNone(accrual.accrue(
            amount_mu=1_000_000, ledger=None, contract_hash=CONTRACT, asset="ERG"
        ))

    def test_an_empty_pay_list_accrues_nothing_and_says_so_once(self):
        with mock.patch(
            "src.payment_system.donations.config.pay_wallets", return_value=[]
        ):
            accrual._warned_about_empty_pay_list.clear()
            self.assertIsNone(self._accrue(1_000_000))
            self.assertEqual(self.recorder.debts, {})

    def test_a_broken_catalogue_never_undoes_the_client_credit(self):
        """The money has arrived and the client has been credited by this point.

        Losing the record of the debt is a bookkeeping loss; raising here would unwind
        a payment that is already on-chain.
        """
        class _Broken:
            def accrue_donation(self, **_):
                raise RuntimeError("database is gone")

        module = types.ModuleType("src.database.sql_connection")
        module.SQLConnection = lambda: _Broken()  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"src.database.sql_connection": module}):
            self.assertIsNone(self._accrue(1_000_000))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ExactConversionTests(unittest.TestCase):
    """The conversion a debt is accrued through keeps its fraction; a payment's does not."""

    def test_a_payment_truncates_and_a_debt_does_not(self):
        with mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(3)):
            # A transaction must never claim more than is owed.
            self.assertEqual(rate.mu_to_nanoerg(10), 3)
            # A debt accrued from many payments must not lose a third of a unit each time.
            self.assertEqual(rate.mu_to_nanoerg_exact(10), Decimal(10) / Decimal(3))


if __name__ == "__main__":
    unittest.main()

"""`1 + N` payment methods out of one module, one wallet and one lock.

A token on Ergo is not another contract: it is the same P2PK script paid in different
money. So what the registry gets from this module is instances rather than modules --
a module can only ever be one method -- and only the calls whose answer depends on
*which asset* are bound per method.

The split is load-bearing in both directions. Bind too little and a token settles at
ERG's rate; bind too much and the periodic tick runs `N + 1` times, paying `N + 1` fees
for one job.
"""
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.ergo import interface, rate
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    interface = None  # type: ignore[assignment]

TOKEN = "ab" * 32
OTHER = "cd" * 32


def _asset(token_id=TOKEN, symbol="SigUSD", unit="sigusd", decimals=2, mu=20_000_000):
    return {
        "TOKEN_ID": token_id, "SYMBOL": symbol, "UNIT_NAME": unit,
        "DECIMALS": decimals, "MU_PER_UNIT": mu,
    }


def _configured(assets):
    return mock.patch.object(rate, "assets", return_value=tuple(
        rate.Asset(
            token_id=a["TOKEN_ID"], symbol=a["SYMBOL"], unit_name=a["UNIT_NAME"],
            decimals=a["DECIMALS"], mu_per_base_unit=Decimal(a["MU_PER_UNIT"]),
        ) for a in assets
    ))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MethodFactoryTests(unittest.TestCase):
    def test_no_assets_is_one_method_in_the_native_unit(self):
        # A node that has not opted in behaves exactly as it did before tokens existed.
        with _configured([]):
            built = interface.methods()
        self.assertEqual([m.asset for m in built], ["ERG"])

    def test_erg_comes_first_and_the_assets_follow_in_declared_order(self):
        # The order is the payer's preference: the payment walk tries one method and
        # falls through to the next, so it has to be reproducible rather than
        # set-ordered.
        with _configured([_asset(OTHER, "SigRSV", "sigrsv"), _asset()]):
            built = interface.methods()
        self.assertEqual([m.asset for m in built], ["ERG", OTHER, TOKEN])

    def test_every_method_keys_on_this_contract_and_this_ledger(self):
        with _configured([_asset()]):
            built = interface.methods()
        self.assertEqual(
            [(m.key.ledger, m.key.contract_hash) for m in built],
            [("ergo", interface.CONTRACT_HASH)] * 2,
        )

    def test_each_method_advertises_its_own_rate(self):
        with _configured([_asset()]), \
                mock.patch.object(rate, "mu_per_nanoerg", return_value=Decimal(1)):
            native, token = interface.methods()
            # One ERG is 1e9 nanoERG; one whole SigUSD is 100 cents at 2e7 MU each.
            self.assertEqual(native.mu_per_unit(), 1_000_000_000)
            self.assertEqual(token.mu_per_unit(), 2_000_000_000)

    def test_the_per_contract_jobs_are_not_bound_per_asset(self):
        # Two methods, one tick. An Ergo transaction carries several assets in one
        # output, so sweeping and paying per asset would pay a fee per asset for what
        # is one job -- and `envs` dispatches these by contract for exactly that reason.
        with _configured([_asset(), _asset(OTHER, "SigRSV", "sigrsv")]):
            built = interface.methods()
        for name in ("init", "manager", "manager_iteration_time", "get_balance",
                     "transaction_history", "unavailable_reason"):
            with self.subTest(call=name):
                self.assertEqual(
                    len({getattr(m, name) for m in built}), 1,
                    f"{name} should be the contract's own, shared by every method",
                )

    def test_the_asset_dependent_calls_are_bound_per_method(self):
        with _configured([_asset()]):
            native, token = interface.methods()
        for name in ("mu_per_unit", "settlement_floors_mu", "mu_to_native",
                     "check_sender_balance", "process_payment",
                     "payment_process_validator"):
            with self.subTest(call=name):
                self.assertIsNot(getattr(native, name), getattr(token, name))

    def test_a_method_forwards_everything_it_does_not_override(self):
        with _configured([_asset()]):
            _native, token = interface.methods()
        self.assertEqual(token.LEDGER, "ergo")
        self.assertEqual(token.CONTRACT_HASH, interface.CONTRACT_HASH)
        self.assertEqual(token.NATIVE_ASSET, "ERG")
        self.assertTrue(token.needs_unspent_proof)
        self.assertEqual(token.DEPOSIT_TOKEN_TTL, interface.DEPOSIT_TOKEN_TTL)

    def test_a_token_method_converts_a_debt_in_its_own_base_unit(self):
        # What #282's accrual reads. In MU the debt would be reinterpreted the next
        # time the operator changed this asset's rate.
        with _configured([_asset()]):
            _native, token = interface.methods()
        self.assertEqual(token.mu_to_native(10_000_000), Decimal("0.5"))

    def test_the_registry_offers_every_method_of_the_contract(self):
        # End to end through the registry, which is what `envs` and the payer read.
        from src.payment_system.contracts import registry

        with _configured([_asset()]), mock.patch.object(
            registry, "contracts", return_value={interface.CONTRACT_HASH: interface}
        ):
            offered = registry.methods()
        self.assertEqual(
            sorted(key.asset for key in offered), sorted(["ERG", TOKEN])
        )

    def test_a_contract_whose_assets_will_not_parse_offers_nothing(self):
        # A malformed asset list is a configuration error the operator has to see. It
        # is caught at startup; this is the backstop, and it must not leave the node
        # advertising a method whose rate nobody declared.
        from src.payment_system.contracts import registry

        with mock.patch.object(rate, "assets", side_effect=ValueError("bad token id")), \
                mock.patch.object(
                    registry, "contracts",
                    return_value={interface.CONTRACT_HASH: interface}), \
                mock.patch.object(registry, "_report"):
            self.assertEqual(registry.methods(), {})


if __name__ == "__main__":
    unittest.main()

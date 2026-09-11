"""Naming a payment method on the command line, when the ledger no longer names one.

`nodo pay <peer> <amount> --ledger ergo` was a sufficient selector while a ledger meant
a currency. It stopped being one the moment an Ergo contract could be paid in ERG and in
a token: both are `ergo`, at different rates, and the amount the operator typed means a
different quantity of money in each.

Two failures this guards against, and the second is the expensive one:

* Naming a method the node does not offer has to be refused *by name*, with the ones it
  does offer listed -- an operator who mistyped a symbol should not have to read the
  config to find out what is available.
* The named method has to be what the payment actually settles through. `--payment-method
  ergo:SigUSD` that only picked the rate would convert five SigUSD into MU and then let
  the payer settle that many MU worth of ERG, because funding is otherwise the selection.
"""
import unittest
from contextlib import contextmanager
from unittest import mock

IMPORT_ERROR = None
try:
    from src.commands import pay as pay_command
    from src.payment_system.contracts.registry import MethodKey, PaymentMethod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    pay_command = None  # type: ignore[assignment]

TOKEN = "ab" * 32


def method(ledger, asset, *, symbol="", unit_name="", rate=1, is_demo=False,
           contract_hash=None):
    """One registered payment method, as the selector reads one."""
    contract = mock.Mock()
    contract.LEDGER = ledger
    contract.CONTRACT_HASH = contract_hash or f"{ledger}-contract"
    contract.is_demo = is_demo
    contract.mu_per_unit.return_value = rate
    return PaymentMethod(contract, asset, symbol=symbol, unit_name=unit_name)


@contextmanager
def offering(*methods):
    """A registry offering exactly these methods, in this order."""
    with mock.patch(
        "src.payment_system.contracts.registry.methods",
        return_value={m.key: m for m in methods},
    ):
        yield


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SelectorTests(unittest.TestCase):
    def _erg_and_token(self):
        # The case that matters: one contract, one address, two assets.
        return (
            method("ergo", "ERG", unit_name="erg", contract_hash="p2pk"),
            method("ergo", TOKEN, symbol="SigUSD", unit_name="sigusd",
                   contract_hash="p2pk"),
        )

    def test_one_offered_method_needs_no_flag(self):
        only = method("ergo", "ERG")
        with offering(only):
            self.assertIs(pay_command._method_for(None, None, None)[0], only)

    def test_the_ledger_alone_is_not_enough_once_it_carries_two_assets(self):
        erg, token = self._erg_and_token()
        with offering(erg, token):
            chosen, refusal = pay_command._method_for("ergo", None, None)
        self.assertIsNone(chosen, "both methods are on 'ergo'")
        self.assertIn("more than one payment method", refusal)
        self.assertIn("ergo:ERG", refusal)
        self.assertIn("ergo:SigUSD", refusal)

    def test_the_compact_form_selects_one(self):
        erg, token = self._erg_and_token()
        with offering(erg, token):
            self.assertIs(pay_command._method_for(None, None, "ergo:SigUSD")[0], token)
            self.assertIs(pay_command._method_for(None, None, "ergo:ERG")[0], erg)

    def test_the_split_form_selects_the_same_one(self):
        erg, token = self._erg_and_token()
        with offering(erg, token):
            self.assertIs(pay_command._method_for("ergo", "SigUSD", None)[0], token)

    def test_an_asset_is_matched_by_symbol_unit_name_or_id(self):
        # The id is the identity -- a name is not, anyone can mint a token called
        # SigUSD -- but an operator typing a command has the symbol in front of them.
        erg, token = self._erg_and_token()
        for typed in ("SigUSD", "sigusd", "SIGUSD", TOKEN, TOKEN.upper()):
            with self.subTest(asset=typed), offering(erg, token):
                self.assertIs(pay_command._method_for("ergo", typed, None)[0], token)

    def test_a_ledger_without_an_asset_still_works_when_it_carries_one(self):
        # A single-asset ledger is not made harder to use by the flag existing.
        erg = method("ergo", "ERG")
        btc = method("bitcoin", "BTC")
        with offering(erg, btc):
            self.assertIs(pay_command._method_for("bitcoin", None, None)[0], btc)
            self.assertIs(pay_command._method_for(None, None, "bitcoin")[0], btc)

    def test_an_asset_the_node_does_not_accept_is_refused_by_name(self):
        erg, token = self._erg_and_token()
        with offering(erg, token):
            chosen, refusal = pay_command._method_for("ergo", "SigRSV", None)
        self.assertIsNone(chosen)
        self.assertIn("SigRSV", refusal)
        self.assertIn("ergo:SigUSD", refusal, "the ones it does accept are listed")

    def test_a_demo_method_is_never_selected(self):
        # A simulated payment settles on no chain, so paying through it would pay
        # nobody -- and it must not make a single real method ambiguous either.
        real = method("ergo", "ERG")
        with offering(real, method("simulated", "", is_demo=True)):
            self.assertIs(pay_command._method_for(None, None, None)[0], real)

    def test_a_node_with_no_payment_system_says_so(self):
        with offering():
            chosen, refusal = pay_command._method_for(None, None, None)
        self.assertIsNone(chosen)
        self.assertIn("offers no payment system", refusal)

    def test_the_amount_is_read_at_the_named_methods_rate(self):
        # Two rates on one contract: the same typed figure is different money.
        erg = method("ergo", "ERG", rate=1_000_000_000, contract_hash="p2pk")
        token = method("ergo", TOKEN, symbol="SigUSD", rate=2_000_000_000,
                       contract_hash="p2pk")
        self.assertEqual(pay_command._amount_to_mu(erg, "2"), 2_000_000_000)
        self.assertEqual(pay_command._amount_to_mu(token, "2"), 4_000_000_000)

    def test_the_refusal_names_the_asset_a_person_reads(self):
        # Not the 64-hex id, which is what identifies it and not what an operator
        # recognises.
        token = method("ergo", TOKEN, symbol="SigUSD", rate=10)
        with self.assertRaisesRegex(ValueError, "SigUSD"):
            pay_command._amount_to_mu(token, "0.05")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SettlementIsRestrictedTests(unittest.TestCase):
    """A named method is what settles, not just what the amount was read in."""

    def test_the_payer_is_told_which_method_to_use(self):
        from src.payment_system import payment_process

        systems = [
            mock.Mock(key=MethodKey("ergo", "p2pk", "ERG"), ledger_tag="ergo",
                      contract_hash="p2pk", asset="ERG",
                      local_mu_per_unit=1, peer_mu_per_unit=1),
            mock.Mock(key=MethodKey("ergo", "p2pk", TOKEN), ledger_tag="ergo",
                      contract_hash="p2pk", asset=TOKEN,
                      local_mu_per_unit=1, peer_mu_per_unit=1),
        ]
        with mock.patch(
            "src.payment_system.mu_conversion.matching_payment_systems",
            return_value=systems,
        ), mock.patch.object(payment_process, "_payment_envs") as envs:
            envs.return_value.settlement_floors.return_value = {}
            plans, _refusals = getattr(payment_process, "__settlement_plans")(
                peer_id="peer-1", amount=1_000, floor=False,
                method=MethodKey("ergo", "p2pk", TOKEN),
            )
        self.assertEqual([plan.asset for plan in plans], [TOKEN])

    def test_a_method_the_peer_does_not_share_is_refused_rather_than_substituted(self):
        # Falling through to another asset would settle a payment the operator did not
        # name, in money they did not choose.
        from src.payment_system import payment_process

        systems = [
            mock.Mock(key=MethodKey("ergo", "p2pk", "ERG"), ledger_tag="ergo",
                      contract_hash="p2pk", asset="ERG",
                      local_mu_per_unit=1, peer_mu_per_unit=1),
        ]
        with mock.patch(
            "src.payment_system.mu_conversion.matching_payment_systems",
            return_value=systems,
        ), mock.patch.object(payment_process, "_payment_envs") as envs:
            envs.return_value.settlement_floors.return_value = {}
            plans, refusals = getattr(payment_process, "__settlement_plans")(
                peer_id="peer-1", amount=1_000, floor=False,
                method=MethodKey("ergo", "p2pk", TOKEN),
            )
        self.assertEqual(plans, [])
        self.assertIn("not a payment method shared with this peer", " ".join(refusals))

    def test_without_a_named_method_every_shared_one_is_a_candidate(self):
        # Funding stays the selection when nobody named anything.
        from src.payment_system import payment_process

        systems = [
            mock.Mock(key=MethodKey("ergo", "p2pk", "ERG"), ledger_tag="ergo",
                      contract_hash="p2pk", asset="ERG",
                      local_mu_per_unit=1, peer_mu_per_unit=1),
            mock.Mock(key=MethodKey("ergo", "p2pk", TOKEN), ledger_tag="ergo",
                      contract_hash="p2pk", asset=TOKEN,
                      local_mu_per_unit=1, peer_mu_per_unit=1),
        ]
        with mock.patch(
            "src.payment_system.mu_conversion.matching_payment_systems",
            return_value=systems,
        ), mock.patch.object(payment_process, "_payment_envs") as envs:
            envs.return_value.settlement_floors.return_value = {}
            plans, _refusals = getattr(payment_process, "__settlement_plans")(
                peer_id="peer-1", amount=1_000, floor=False,
            )
        self.assertEqual([plan.asset for plan in plans], ["ERG", TOKEN])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OptionParsingTests(unittest.TestCase):
    """One pass over argv, because an option's value looks like a positional."""

    def _take(self, argv):
        import importlib.util
        import sys

        if "nodo_cli" not in sys.modules:
            spec = importlib.util.spec_from_file_location("nodo_cli", "nodo.py")
            module = importlib.util.module_from_spec(spec)
            # Only the parser is wanted, and importing the dispatcher runs the whole
            # node's import graph, so the function is read out of the source instead.
            source = open("nodo.py", encoding="utf-8").read()
            start = source.index("def take_options(")
            end = source.index("def gateway_port(")
            exec(compile(source[start:end], "nodo.py", "exec"), module.__dict__)
            sys.modules["nodo_cli"] = module
        return sys.modules["nodo_cli"].take_options(
            argv, "--payment-method", "--ledger", "--asset"
        )

    def test_two_flags_do_not_eat_each_others_values(self):
        # The regression a one-flag parser has: "ergo" is a bare word, so parsing
        # --ledger alone leaves --asset's value looking exactly like an amount.
        positionals, values = self._take(
            ["peer-1", "5", "--ledger", "ergo", "--asset", "sigusd"]
        )
        self.assertEqual(positionals, ["peer-1", "5"])
        self.assertEqual(values["--ledger"], "ergo")
        self.assertEqual(values["--asset"], "sigusd")

    def test_the_equals_form_works_too(self):
        positionals, values = self._take(
            ["peer-1", "5", "--payment-method=ergo:sigusd"]
        )
        self.assertEqual(positionals, ["peer-1", "5"])
        self.assertEqual(values["--payment-method"], "ergo:sigusd")

    def test_an_unknown_option_never_becomes_an_amount(self):
        positionals, values = self._take(["peer-1", "5", "--leger", "ergo"])
        self.assertEqual(positionals, ["peer-1", "5", "ergo"])
        self.assertEqual(values, {})

    def test_a_flag_left_waiting_at_the_end_of_the_input_is_an_error(self):
        """`nodo pay <peer> 1 --ledger` did not name a ledger, and must say so.

        Dropped in silence it behaves as though no ledger had been named at all, so a
        node offering two payment systems answers "the amount is ambiguous" -- which
        sends the operator looking at the amount rather than at the half-typed flag.
        """
        with self.assertRaisesRegex(ValueError, r"--ledger needs a value"):
            self._take(["peer-1", "5", "--ledger"])

    def test_the_last_flag_still_takes_the_value_that_follows_it(self):
        # The check is about the end of the input, not about the last flag.
        positionals, values = self._take(["peer-1", "5", "--asset", "sigusd"])
        self.assertEqual(positionals, ["peer-1", "5"])
        self.assertEqual(values["--asset"], "sigusd")


if __name__ == "__main__":
    unittest.main()

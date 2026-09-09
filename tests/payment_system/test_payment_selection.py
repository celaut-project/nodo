"""Funding is the selection: the payer settles through the first system it can fund.

There is no preference policy and none is needed. What has to hold is that the walk
happens at all -- sharing two currencies used to *raise*, so two nodes that both
accepted ERG and BTC could not pay each other -- and that the figures follow the
contract that actually settled rather than the one that was resolved first.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.payment_system import payment_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    payment_process = None  # type: ignore[assignment]

FIRST = "first-contract"
SECOND = "second-contract"
# A token id, which is what an asset is: 64 hex characters, never a name.
TOKEN = "ab" * 32
SCRIPT = bytes.fromhex("0008cd03" + "77" * 32)


def _key(contract_hash, ledger, asset):
    """The triple the dispatch is keyed by: ledger, contract and asset."""
    from src.payment_system.contracts.registry import MethodKey

    return MethodKey(ledger, contract_hash, asset)


FIRST_KEY = None if IMPORT_ERROR else _key(FIRST, "ergo", "ERG")
SECOND_KEY = None if IMPORT_ERROR else _key(SECOND, "bitcoin", "BTC")


class _Envs:
    """A registry with two payment systems, each with its own funding and figures."""

    DEMOS = ()

    def __init__(self, funded, keys=None):
        self.funded = set(funded)
        self.settled = []
        # Keyed by the *method*: on Ergo two of these differ only in their asset, and
        # `funded` names whichever part of the key the test is about.
        self.keys = keys or {FIRST: FIRST_KEY, SECOND: SECOND_KEY}

    def available_payment_process(self):
        def process(name):
            def process_payment(amount, deposit_token, ledger, script):
                self.settled.append((name, amount))
                return celaut_pb2.Contract(ledger=ledger)
            return process_payment

        return {key: process(name) for name, key in self.keys.items()}

    def check_sender_balances(self):
        return {
            key: (lambda name: lambda amount: name in self.funded)(name)
            for name, key in self.keys.items()
        }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PaymentSelectionTests(unittest.TestCase):

    def _pay(self, funded, plans=None, keys=None):
        envs = _Envs(funded, keys)
        ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="", formal=b"")
        told = []
        plans = plans or [
            payment_process.SettlementPlan(contract_hash=FIRST, ledger_tag="ergo",
                                           asset="ERG", amount=1_000, peer_amount=2_000),
            payment_process.SettlementPlan(contract_hash=SECOND, ledger_tag="bitcoin",
                                           asset="BTC", amount=1_000, peer_amount=7),
        ]
        with mock.patch.object(payment_process, "_payment_envs", return_value=envs), \
                mock.patch.object(payment_process, "sc", mock.MagicMock()), \
                mock.patch.object(payment_process, "get_peer_contract_instances",
                                  side_effect=lambda *a, **k: iter([(SCRIPT, ledger, "ERG")])), \
                mock.patch.object(payment_process, "ledger_balancer",
                                  side_effect=lambda ledger_generator: ledger_generator), \
                mock.patch.object(payment_process, "_reputation_interface"), \
                mock.patch.object(payment_process, "__obtain_deposit_token",
                                  return_value="deposit-token-1", create=True), \
                mock.patch.object(
                    payment_process, "__attempt_payment_communication", create=True,
                    side_effect=lambda peer_id, amount, token, contract: (
                        told.append(amount) or True
                    )):
            settled = getattr(payment_process, "__peer_payment_process")(
                peer_id="peer-1", plans=plans
            )
        return settled, envs, told

    def _asset_plans(self):
        """Two methods of ONE contract, differing only in their asset."""
        return [
            payment_process.SettlementPlan(contract_hash=FIRST, ledger_tag="ergo",
                                           asset="ERG", amount=1_000, peer_amount=2_000),
            payment_process.SettlementPlan(contract_hash=FIRST, ledger_tag="ergo",
                                           asset=TOKEN, amount=1_000, peer_amount=50),
        ]

    def test_the_first_funded_system_settles(self):
        settled, envs, _ = self._pay(funded={FIRST, SECOND})
        self.assertEqual(settled.contract_hash, FIRST)
        self.assertEqual([hash_ for hash_, _ in envs.settled], [FIRST])

    def test_an_unfunded_first_system_falls_through_to_the_second(self):
        """Funding *is* the selection.

        A node holding ERG and no BTC pays a peer that accepts both in ERG, with no
        policy and no setting to that effect.
        """
        settled, envs, _ = self._pay(funded={SECOND})
        self.assertEqual(settled.contract_hash, SECOND)
        self.assertEqual([hash_ for hash_, _ in envs.settled], [SECOND])

    def test_the_peer_is_told_the_figure_of_the_system_that_settled(self):
        """The regression this restructure exists for.

        Both figures used to be resolved once, from whichever system was picked first,
        and handed to a loop free to settle through another. With two systems the node
        pays over one chain and claims a credit converted at the other's rate, so the
        peer's validator rejects a payment that is already on-chain.
        """
        _, _, told = self._pay(funded={SECOND})
        self.assertEqual(told, [7], "the peer was told the first system's figure")

    def test_no_funded_system_pays_nothing_and_says_so(self):
        settled, envs, told = self._pay(funded=set())
        self.assertIsNone(settled)
        self.assertEqual(envs.settled, [])
        self.assertEqual(told, [])

    def test_two_assets_on_one_contract_are_two_candidates(self):
        """The case the asset dimension exists for, on the paying side.

        Same ledger, same contract, same script, same address: only the money differs.
        Keyed by the contract these two plans would dispatch to the same
        implementation, so the node could never pay in the second asset -- and the
        fall-through below would have nothing to fall through to.
        """
        keys = {"erg": FIRST_KEY, "token": _key(FIRST, "ergo", TOKEN)}
        settled, envs, told = self._pay(funded={"token"}, plans=self._asset_plans(),
                                        keys=keys)
        self.assertEqual([name for name, _ in envs.settled], ["token"])
        # And the peer is told the figure of the asset that settled, not the first
        # asset's -- the same regression as between two ledgers, one contract inside.
        self.assertEqual(told, [50])

    def test_the_asset_the_wallet_can_fund_is_the_one_used(self):
        # A wallet out of SigUSD but holding ERG pays in ERG, with no policy and no
        # new setting: funding is the selection here too.
        keys = {"erg": FIRST_KEY, "token": _key(FIRST, "ergo", TOKEN)}
        _settled, envs, told = self._pay(funded={"erg"}, plans=self._asset_plans(),
                                         keys=keys)
        self.assertEqual([name for name, _ in envs.settled], ["erg"])
        self.assertEqual(told, [2_000])

    def test_the_candidate_order_is_the_plan_order(self):
        # Reproducible rather than set-ordered: both funded, the first declared wins.
        keys = {"erg": FIRST_KEY, "token": _key(FIRST, "ergo", TOKEN)}
        for funded in ({"erg", "token"},):
            _settled, envs, _told = self._pay(funded=funded, plans=self._asset_plans(),
                                              keys=keys)
            self.assertEqual([name for name, _ in envs.settled], ["erg"])

    def test_only_the_settling_assets_instances_are_read(self):
        """The rows of one asset are the right address for the wrong money.

        On Ergo every asset of a contract shares a script and an address, so what tells
        the payer which instance to build its output against is the asset -- and asking
        for the contract's rows would hand it another method's.
        """
        keys = {"erg": FIRST_KEY, "token": _key(FIRST, "ergo", TOKEN)}
        asked = []
        ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="", formal=b"")

        def rows(contract_hash, peer_id, asset=None):
            asked.append((contract_hash, asset))
            return iter([(SCRIPT, ledger, asset or "")])

        envs = _Envs({"token"}, keys)
        with mock.patch.object(payment_process, "_payment_envs", return_value=envs), \
                mock.patch.object(payment_process, "sc", mock.MagicMock()), \
                mock.patch.object(payment_process, "get_peer_contract_instances",
                                  side_effect=rows), \
                mock.patch.object(payment_process, "ledger_balancer",
                                  side_effect=lambda ledger_generator: ledger_generator), \
                mock.patch.object(payment_process, "_reputation_interface"), \
                mock.patch.object(payment_process, "__obtain_deposit_token",
                                  return_value="deposit-token-1", create=True), \
                mock.patch.object(
                    payment_process, "__attempt_payment_communication", create=True,
                    return_value=True):
            getattr(payment_process, "__peer_payment_process")(
                peer_id="peer-1", plans=self._asset_plans()
            )
        # The unfunded ERG method never reaches the database at all, and the token one
        # asks for its own asset by name.
        self.assertEqual(asked, [(FIRST, TOKEN)])

    def test_a_token_id_in_either_case_is_the_same_method(self):
        # The dispatch is a dict lookup, so a case difference is not a near miss: it is
        # a method that does not exist. `MethodKey` folds an id's case for that reason,
        # and only an id's -- a native symbol travels as advertised.
        self.assertEqual(_key(FIRST, "ergo", TOKEN.upper()), _key(FIRST, "ergo", TOKEN))
        self.assertNotEqual(_key(FIRST, "ergo", "erg"), _key(FIRST, "ergo", "ERG"))

    def test_a_system_the_node_cannot_process_is_skipped(self):
        # Shared with the peer a moment ago and not offered now: a runtime that went
        # away between matching and paying.
        plans = [
            payment_process.SettlementPlan(contract_hash="vanished", ledger_tag="ergo",
                                           asset="ERG", amount=1_000, peer_amount=2_000),
            payment_process.SettlementPlan(contract_hash=SECOND, ledger_tag="bitcoin",
                                           asset="BTC", amount=1_000, peer_amount=7),
        ]
        settled, envs, _ = self._pay(funded={SECOND}, plans=plans)
        self.assertEqual(settled.contract_hash, SECOND)


if __name__ == "__main__":
    unittest.main()

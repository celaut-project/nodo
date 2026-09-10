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
SCRIPT = bytes.fromhex("0008cd03" + "77" * 32)


class _Envs:
    """A registry with two payment systems, each with its own funding and figures."""

    DEMOS = ()

    def __init__(self, funded):
        self.funded = set(funded)
        self.settled = []

    def available_payment_process(self):
        def process(contract_hash):
            def process_payment(amount, deposit_token, ledger, script):
                self.settled.append((contract_hash, amount))
                return celaut_pb2.Contract(ledger=ledger)
            return process_payment

        return {FIRST: process(FIRST), SECOND: process(SECOND)}

    def check_sender_balances(self):
        return {
            FIRST: lambda amount: FIRST in self.funded,
            SECOND: lambda amount: SECOND in self.funded,
        }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PaymentSelectionTests(unittest.TestCase):

    def _pay(self, funded, plans=None):
        envs = _Envs(funded)
        ledger = celaut_pb2.Contract.Ledger(tags=["ergo"], prose="", formal=b"")
        told = []
        plans = plans or [
            payment_process.SettlementPlan(contract_hash=FIRST, ledger_tag="ergo",
                                           amount=1_000, peer_amount=2_000),
            payment_process.SettlementPlan(contract_hash=SECOND, ledger_tag="bitcoin",
                                           amount=1_000, peer_amount=7),
        ]
        with mock.patch.object(payment_process, "_payment_envs", return_value=envs), \
                mock.patch.object(payment_process, "sc", mock.MagicMock()), \
                mock.patch.object(payment_process, "get_peer_contract_instances",
                                  side_effect=lambda *a, **k: iter([(SCRIPT, ledger)])), \
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

    def test_a_system_the_node_cannot_process_is_skipped(self):
        # Shared with the peer a moment ago and not offered now: a runtime that went
        # away between matching and paying.
        plans = [
            payment_process.SettlementPlan(contract_hash="vanished", ledger_tag="ergo",
                                           amount=1_000, peer_amount=2_000),
            payment_process.SettlementPlan(contract_hash=SECOND, ledger_tag="bitcoin",
                                           amount=1_000, peer_amount=7),
        ]
        settled, envs, _ = self._pay(funded={SECOND}, plans=plans)
        self.assertEqual(settled.contract_hash, SECOND)


if __name__ == "__main__":
    unittest.main()

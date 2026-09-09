"""How long a deposit token lives, and which contracts wait for one.

Two Ergo-shaped constants sat around confirmation, and both break the moment a slower
chain exists: a single global TTL, and a pause applied to every contract.

The pause is genuinely per contract and is fixed here. The TTL is **not**, and the
reason is worth stating rather than pretending otherwise: a token is issued by the
*receiver*, in `GenerateDepositToken`, before the payer has chosen a payment system.
The row has no contract on it and cannot have one without a change to the wire. So
there is one deadline, and it takes the maximum -- long enough for the slowest chain
someone might pay this node on.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system import payment_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    payment_process = None  # type: ignore[assignment]

FAST = "fast-contract"     # Ergo-like: two-minute blocks, needs an unspent output
SLOW = "slow-contract"     # Bitcoin-like: ten-minute blocks, proof is a confirmed tx


def _envs(ttls, pausing=(), managers=None):
    return type("envs", (), {
        "deposit_token_ttls": staticmethod(lambda: dict(ttls)),
        "needs_unspent_proof": staticmethod(lambda: tuple(pausing)),
        "manage_interfaces": staticmethod(lambda: dict(managers or {})),
    })


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DepositTokenTtlTests(unittest.TestCase):

    def test_the_deadline_is_the_slowest_chain_the_node_accepts(self):
        """Too short refuses a payment that is already on-chain.

        The two directions are not symmetrical: a token that expires early rejects an
        honest payer whose money has already left, while one that expires late only
        delays this node's own sweep, which pays nobody.
        """
        with mock.patch.object(payment_process, "_payment_envs",
                               return_value=_envs({FAST: 3600, SLOW: 21600})):
            self.assertEqual(payment_process.deposit_token_ttl(), 21600)

    def test_one_contract_gives_its_own_figure(self):
        with mock.patch.object(payment_process, "_payment_envs",
                               return_value=_envs({FAST: 3600})):
            self.assertEqual(payment_process.deposit_token_ttl(), 3600)

    def test_no_contract_at_all_falls_back_rather_than_raising(self):
        # Read from the manager tick; a registry that answers nothing must not stop it.
        with mock.patch.object(payment_process, "_payment_envs", return_value=_envs({})):
            self.assertEqual(
                payment_process.deposit_token_ttl(),
                payment_process.DEFAULT_DEPOSIT_TOKEN_TTL,
            )

    def test_the_ergo_and_bitcoin_contracts_really_do_differ(self):
        # The premise of all of the above: if both chains declared the same figure there
        # would be nothing to take a maximum of.
        from src.payment_system.contracts.bitcoin import interface as btc
        from src.payment_system.contracts.ergo import interface as ergo

        self.assertGreater(btc.DEPOSIT_TOKEN_TTL, ergo.DEPOSIT_TOKEN_TTL)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SweepPauseTests(unittest.TestCase):
    """Only the contracts that prove payment from an unspent output wait for a drain."""

    def _tick(self, *, pausing, drained=True):
        ran = []
        managers = {
            FAST: lambda: ran.append(FAST),
            SLOW: lambda: ran.append(SLOW),
        }
        envs = _envs({FAST: 3600, SLOW: 21600}, pausing=pausing, managers=managers)
        with mock.patch.object(payment_process, "_payment_envs", return_value=envs), \
                mock.patch.object(payment_process, "_pause_and_drain_deposits",
                                  return_value=drained), \
                mock.patch.object(payment_process, "sc", mock.MagicMock()), \
                mock.patch.object(payment_process, "sleep",
                                  side_effect=[None, StopIteration]):
            manage = getattr(payment_process, "__manage_interfaces")
            try:
                manage()
            except StopIteration:
                pass
        return ran

    def test_a_contract_that_needs_no_unspent_output_runs_regardless(self):
        """The whole point of making the pause per contract.

        A chain whose confirmations take longer than the drain timeout would otherwise
        never reach zero pending on a busy node, so its donations and its cold sweep
        would simply never happen.
        """
        ran = self._tick(pausing=(FAST,), drained=False)
        self.assertEqual(ran, [SLOW])

    def test_both_run_when_the_deposits_drain(self):
        ran = self._tick(pausing=(FAST,), drained=True)
        self.assertEqual(sorted(ran), sorted([FAST, SLOW]))

    def test_a_pending_deposit_still_holds_back_the_contract_that_needs_it(self):
        # Ergo's sweep spends the very box its validator has to find unspent, so a
        # sweep that runs anyway turns an honest payment into a rejected one.
        self.assertNotIn(FAST, self._tick(pausing=(FAST,), drained=False))

    def test_nothing_pauses_when_no_contract_asks_for_it(self):
        ran = self._tick(pausing=(), drained=False)
        self.assertEqual(sorted(ran), sorted([FAST, SLOW]))

    def test_a_registry_that_cannot_answer_pauses_everything(self):
        # The cautious direction: a sweep that runs when it should not have can cost a
        # client an honest payment, while an extra wait costs a delay.
        broken = type("envs", (), {
            "deposit_token_ttls": staticmethod(dict),
            "needs_unspent_proof": staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no registry"))),
            "manage_interfaces": staticmethod(lambda: {FAST: lambda: None}),
        })
        with mock.patch.object(payment_process, "_payment_envs", return_value=broken), \
                mock.patch.object(payment_process, "_pause_and_drain_deposits",
                                  return_value=False) as drain, \
                mock.patch.object(payment_process, "sc", mock.MagicMock()), \
                mock.patch.object(payment_process, "sleep",
                                  side_effect=[None, StopIteration]):
            manage = getattr(payment_process, "__manage_interfaces")
            try:
                manage()
            except StopIteration:
                pass
        drain.assert_called_once()


if __name__ == "__main__":
    unittest.main()

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


def _envs(ttls, pausing=(), managers=None, intervals=None):
    return type("envs", (), {
        "deposit_token_ttls": staticmethod(lambda: dict(ttls)),
        "needs_unspent_proof": staticmethod(lambda: tuple(pausing)),
        "manage_interfaces": staticmethod(lambda: dict(managers or {})),
        "manager_iteration_times": staticmethod(lambda: dict(intervals or {})),
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


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ManagerIntervalTests(unittest.TestCase):
    """Each contract's periodic job runs on its own schedule.

    The loop used to read one figure out of **Ergo's** config block and apply it to
    everybody -- so a second ledger's own interval, declared in its own block, was
    ignored, and a node with no `ledgers.ergo` block could not even import the payment
    orchestrator.
    """

    def test_the_loop_wakes_at_the_shortest_interval_asked_for(self):
        self.assertEqual(
            payment_process._manager_quantum({FAST: 3600, SLOW: 86400}), 3600
        )

    def test_no_contract_asking_falls_back_rather_than_busy_looping(self):
        self.assertEqual(
            payment_process._manager_quantum({}),
            payment_process.DEFAULT_MANAGER_ITERATION_TIME,
        )

    def test_a_misconfigured_second_is_floored(self):
        # A tick a minute, not a busy loop: the job spends real money and reads a chain.
        self.assertEqual(
            payment_process._manager_quantum({FAST: 1}),
            payment_process.MIN_MANAGER_QUANTUM,
        )

    def test_a_contract_runs_only_when_its_own_interval_has_elapsed(self):
        managers = {FAST: lambda: None, SLOW: lambda: None}
        intervals = {FAST: 3600, SLOW: 86400}
        last_run = {FAST: 0.0, SLOW: 0.0}

        # An hour in: only the fast one is due.
        due = payment_process._due_managers(managers, intervals, last_run, 3600.0)
        self.assertEqual(set(due), {FAST})

        # A day in: both.
        due = payment_process._due_managers(managers, intervals, last_run, 86400.0)
        self.assertEqual(set(due), {FAST, SLOW})

    def test_a_contract_that_declares_no_interval_runs_every_pass(self):
        # The old behaviour for anything that does not say.
        due = payment_process._due_managers(
            {FAST: lambda: None}, {}, {FAST: 0.0}, 1.0
        )
        self.assertEqual(set(due), {FAST})

    def test_a_contract_is_only_recorded_as_run_once_it_has_run(self):
        last_run: dict = {}
        payment_process._run_managers({FAST: lambda: None}, last_run)
        self.assertIn(FAST, last_run)

    def test_a_job_that_raised_still_counts_as_its_turn(self):
        # What the interval bounds is how often this node spends money reading a
        # chain; a contract whose backend is down must not be retried every quantum.
        last_run: dict = {}
        with mock.patch.object(payment_process._l, "LOGGER"):
            payment_process._run_managers(
                {FAST: lambda: (_ for _ in ()).throw(RuntimeError("no node"))}, last_run
            )
        self.assertIn(FAST, last_run)

    def test_a_drain_that_times_out_does_not_defer_the_paused_contract(self):
        """The stamp used to go on before the drain, so a timeout cost a whole interval.

        86400 s by default, and with Bitcoin registered it stops being rare:
        `deposit_token_ttl()` is the maximum across contracts, so one pending Bitcoin
        deposit holds the pending set non-empty for hours -- and Ergo's donation payout
        and cold sweep are skipped for a day each time it does.
        """
        managers = {FAST: lambda: None, SLOW: lambda: None}
        envs = _envs({FAST: 3600, SLOW: 21600}, pausing=(FAST,), managers=managers,
                     intervals={FAST: 3600, SLOW: 3600})
        with mock.patch.object(payment_process, "_payment_envs", return_value=envs), \
                mock.patch.object(payment_process, "_pause_and_drain_deposits",
                                  return_value=False), \
                mock.patch.object(payment_process, "sc", mock.MagicMock()), \
                mock.patch.object(payment_process, "_run_managers") as run, \
                mock.patch.object(payment_process, "sleep",
                                  side_effect=[None, StopIteration]):
            manage = getattr(payment_process, "__manage_interfaces")
            try:
                manage()
            except StopIteration:
                pass

        # The unpaused contract was handed the map to stamp itself in; the paused one
        # never reached `_run_managers` at all, so nothing recorded it as having run.
        run.assert_called_once()
        ran, last_run = run.call_args.args
        self.assertEqual(set(ran), {SLOW})
        self.assertEqual(last_run, {})

    def test_the_two_shipped_contracts_read_their_own_config_keys(self):
        import inspect

        from src.payment_system.contracts.bitcoin import interface as btc
        from src.payment_system.contracts.ergo import interface as ergo

        self.assertIn(
            "ledgers.ergo.payments.PAYMENT_MANAGER_ITERATION_TIME",
            inspect.getsource(ergo.manager_iteration_time),
        )
        self.assertIn(
            "ledgers.bitcoin.payments.PAYMENT_MANAGER_ITERATION_TIME",
            inspect.getsource(btc.manager_iteration_time),
        )

    def test_the_orchestrator_no_longer_names_a_ledger_for_this(self):
        # The module-level read of Ergo's key is what crashed a node that removed the
        # `ledgers.ergo` block: `int(None)`.
        self.assertFalse(hasattr(payment_process, "PAYMENT_MANAGER_ITERATION_TIME"))


if __name__ == "__main__":
    unittest.main()

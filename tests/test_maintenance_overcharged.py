"""The maintenance that a peer charges a delegated child, against its quote (#567).

The father pays the price of this node (#577), so what the peer charges for the child is
the cost of the delegation. The peer signed a quote for one maintenance period. The
delegated row keeps it in the MU of the peer, and each statement of the child that is
checked is an interval: the cost of its ``maintenance`` movements per second that they
priced, against ``max_maintenance_cost`` per ``maintenance_seconds_loop`` times
``1 + variance``. A peer above it loses ``maintenance_overcharged``. Nothing else
changes: not the instance, not its charge, and not the score of the service or the
client.
"""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.balancers.execution_balancer import execution_balancer as balancer_mod
    from src.database import ledger
    from src.gateway.launcher import launch_service as launch_service_mod
    from src.gateway.launcher.delegate_execution import delegate_execution as delegate_mod
    from src.manager import maintain, statement_check
    from src.manager.statement_check import MaintenanceQuote, Row
    from src.reputation_system.reasons import Reason
    from src.utils.cost_functions import peer_quotes
    from tests.statement_chain import FakeLedger
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


PEER_ID = "peer-a"
PEER_TOKEN = "token-as-the-peer-knows-it"
OUR_ALIAS = "our-alias"
FATHER = "client-a"
SHAPE = {"mem_limit": 1 << 30, "disk_space": 2 << 30, "cpu_period": 100_000,
         "cpu_quota": 100_000}
# The quote of the peer: at most 100 of its MU for a period of 10 s.
QUOTE = {"quote_init_maintenance_cost": 60, "quote_max_maintenance_cost": 100,
         "quote_maintenance_seconds_loop": 10, "quote_variance": 0.0}


def _period(seconds=10, **resources):
    return json.dumps(dict(SHAPE, seconds=seconds, **resources))


def _book(*periods, gap=10):
    """A child: its start and build, then one ``maintenance`` movement for each
    ``(amount, detail)`` in ``periods``, ``gap`` seconds apart."""
    book = FakeLedger(PEER_TOKEN)
    book.add(10_000, "start")
    book.add(-300, "build")
    for amount, detail in periods:
        book.add(-amount, "maintenance", "", detail, book.ts + gap)
    return book


def _row(book, quote=QUOTE, **overrides):
    mark = book.mark_at(2)
    row = {'token': PEER_TOKEN, 'id': OUR_ALIAS, 'peer_id': PEER_ID, 'father_id': FATHER,
           'balance_mu': 10 ** 9, 'peer_balance_mu': mark.balance,
           'peer_statement_seq': 2, 'peer_statement_hash': mark.hash,
           'peer_statement_ts': mark.ts, 'verified_consumption_peer_mu': 0,
           'verified_father_net_peer_mu': 0, 'father_net_peer_mu': 0, 'charged_peer_mu': 0,
           'arch': None, 'charged_until': time.time() - 10,
           'max_mem_limit': SHAPE["mem_limit"] * 2, 'max_disk_space': SHAPE["disk_space"],
           'max_cpu_period': 100_000, 'max_cpu_quota': 200_000,
           **SHAPE, **{f"base_{k}": v for k, v in SHAPE.items()}}
    if quote:
        row.update(quote)
    row.update(overrides)
    return row


def _fetch_of(book):
    return MagicMock(side_effect=lambda peer, client, token="", from_seq=0, rows=None:
                     book.statement(from_seq=from_seq, ts=int(time.time())))


def _rows(*periods, start_ts=1000, gap=10, first_seq=3):
    """``Row``s of ``maintenance`` movements from ``(amount, seconds)``."""
    return [Row(seq=first_seq + i, ts=start_ts + gap * (i + 1), amount=-amount, balance_after=0,
                kind="maintenance", ref="", detail={"seconds": seconds})
            for i, (amount, seconds) in enumerate(periods)]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OverchargeRuleTests(unittest.TestCase):
    """``statement_check.maintenance_overcharge``: the rule, on the scale of the peer."""

    def setUp(self):
        self.quote = MaintenanceQuote(max_mu=100, seconds=10, variance=0.0)

    def test_a_charge_at_most_the_quote_is_not_an_overcharge(self):
        rows = _rows((100, 10), (100, 10), (80, 10))

        self.assertIsNone(statement_check.maintenance_overcharge(rows, self.quote, 1000))

    def test_a_charge_above_the_quote_is_an_overcharge_at_the_first_period(self):
        rows = _rows((100, 10), (150, 10), (150, 10))

        seq, problem = statement_check.maintenance_overcharge(rows, self.quote, 1000)

        self.assertEqual(seq, 3)
        self.assertIn("400 MU", problem)

    def test_the_variance_of_the_quote_is_allowed(self):
        rows = _rows((140, 10), (140, 10))

        self.assertIsNone(statement_check.maintenance_overcharge(
            rows, MaintenanceQuote(100, 10, 0.5), 1000))
        self.assertIsNotNone(statement_check.maintenance_overcharge(
            rows, MaintenanceQuote(100, 10, 0.2), 1000))

    def test_the_rounding_of_each_period_is_allowed(self):
        """A period of 3 s at 100 MU per 10 s is 30 MU, and the peer may round it up."""
        self.assertIsNone(statement_check.maintenance_overcharge(
            _rows((31, 3)), self.quote, 1000))
        self.assertIsNotNone(statement_check.maintenance_overcharge(
            _rows((32, 3)), self.quote, 1000))

    def test_a_long_interval_is_priced_by_its_seconds_and_not_by_the_time_between_reads(self):
        """The peer could not be read for an hour: the statement shows 360 periods."""
        rows = _rows(*[(100, 10)] * 360)

        self.assertIsNone(statement_check.maintenance_overcharge(rows, self.quote, 1000))

    def test_seconds_that_did_not_pass_do_not_lower_the_price(self):
        """Three periods dated 10 s apart that say 100 s each are 30 s and one period."""
        rows = _rows((1000, 100), (1000, 100), (1000, 100))

        self.assertIsNotNone(statement_check.maintenance_overcharge(rows, self.quote, 1000))
        # With no date of the mark the seconds are what the movements say.
        self.assertIsNone(statement_check.maintenance_overcharge(rows, self.quote, None))

    def test_a_period_that_says_no_seconds_charges_for_no_time(self):
        rows = [Row(seq=3, ts=1010, amount=-100, balance_after=0, kind="maintenance", ref="")]

        self.assertIsNotNone(statement_check.maintenance_overcharge(rows, self.quote, 1000))

    def test_only_maintenance_is_judged(self):
        rows = _rows((100, 10)) + [
            Row(seq=4, ts=1020, amount=-5000, balance_after=0, kind="tunnel", ref=""),
            Row(seq=5, ts=1020, amount=-5000, balance_after=0, kind="resource_change", ref=""),
            Row(seq=6, ts=1020, amount=-5000, balance_after=0, kind="statement_query", ref=""),
        ]

        self.assertIsNone(statement_check.maintenance_overcharge(rows, self.quote, 1000))

    def test_no_quote_is_no_limit(self):
        rows = _rows((10 ** 9, 10))

        self.assertIsNone(statement_check.maintenance_overcharge(rows, None, 1000))
        self.assertIsNone(MaintenanceQuote.of_row({}))
        # A node quotes 0 for a service with no `at_most`: that is no ceiling.
        self.assertIsNone(MaintenanceQuote.of_row(dict(QUOTE, quote_max_maintenance_cost=0)))
        self.assertIsNone(MaintenanceQuote.of_row(dict(QUOTE, quote_maintenance_seconds_loop=0)))
        self.assertEqual(MaintenanceQuote.of_row(QUOTE), MaintenanceQuote(100, 10, 0.0))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReadChildAgainstTheQuoteTests(unittest.TestCase):
    """The check runs on what ``_read_child`` read, and changes nothing that it returns."""

    def _read(self, book, row, rates=None):
        sc = MagicMock()
        sc.child_statement_due.return_value = True
        with patch.object(maintain, "sc", sc), \
                patch.object(statement_check, "read", _fetch_of(book)), \
                patch.object(maintain, "_balance_held_on", return_value=10 ** 9), \
                patch.object(statement_check, "stored_rates", return_value=rates or {}), \
                patch.object(maintain, "get_client_id_on_other_peer", return_value="client-on-peer"), \
                patch.object(statement_check, "settle_failed_reads"), \
                patch.object(statement_check, "penalise_no_statement"), \
                patch.object(statement_check, "penalise_once") as penalise:
            result = maintain._read_child(dict(row))
        return result, penalise

    def _reasons(self, penalise):
        return [c.args[2] for c in penalise.call_args_list]

    def test_a_peer_that_charges_at_most_its_quote_is_not_penalised(self):
        book = _book((100, _period()), (90, _period()), (100, _period()))

        _, penalise = self._read(book, _row(book))

        penalise.assert_not_called()

    def test_a_peer_that_charges_more_than_its_quote_is_penalised(self):
        book = _book((100, _period()), (180, _period()), (180, _period()))

        _, penalise = self._read(book, _row(book))

        self.assertEqual(self._reasons(penalise), [Reason.MAINTENANCE_OVERCHARGED])
        args, kwargs = penalise.call_args
        self.assertEqual(args[:2], (PEER_ID, PEER_TOKEN))
        self.assertEqual(args[4], 3)  # the first period of the interval
        self.assertEqual(kwargs, {"window": statement_check.FAULT_WINDOW_SECONDS,
                                  "per_account": True})

    def test_what_the_tick_gets_is_the_same_with_or_without_the_check(self):
        book = _book((100, _period()), (180, _period()), (180, _period()))

        (charge, state), _ = self._read(book, _row(book))
        (charge_unchecked, state_unchecked), penalise = self._read(book, _row(book, quote=None))

        self.assertEqual((charge, state), (charge_unchecked, state_unchecked))
        self.assertEqual(charge, 460)
        penalise.assert_not_called()

    def test_a_row_without_a_quote_keeps_working_and_is_not_penalised(self):
        book = _book((5000, _period()))
        row = _row(book, quote=None)
        for key in QUOTE:
            row[key] = None

        (charge, state), penalise = self._read(book, row)

        penalise.assert_not_called()
        self.assertEqual(charge, 5000)
        self.assertEqual(state["seq"], 3)

    def test_a_hotplug_within_the_most_of_the_quote_is_not_penalised(self):
        """The child doubled its memory on the peer, and pays more, within the most."""
        grown = _period(mem_limit=SHAPE["mem_limit"] * 2)
        book = _book((60, _period()), (100, grown), (100, grown))

        _, penalise = self._read(book, _row(book))

        penalise.assert_not_called()

    def test_the_quote_signed_for_the_instance_counts_not_the_rates_of_today(self):
        """The peer raised its prices after the delegation, and charges the new ones."""
        book = _book((150, _period()), (150, _period()))
        repriced = {"statement_query_mu": 0, "statement_row_mu": 0, "mem_mu": 10 ** 9}

        _, penalise = self._read(book, _row(book), rates=repriced)

        self.assertEqual(self._reasons(penalise), [Reason.MAINTENANCE_OVERCHARGED])

    def test_a_statement_that_does_not_continue_the_mark_is_not_judged_for_maintenance(self):
        """Its movements cannot be checked: the balance is taken, and only the chain is
        scored."""
        book = _book((180, _period()), (180, _period()))
        row = _row(book, peer_statement_hash="another-hash")

        _, penalise = self._read(book, row)

        self.assertEqual(self._reasons(penalise), [Reason.STATEMENT_INCONSISTENT])

    def test_a_check_that_fails_never_breaks_the_reading(self):
        book = _book((180, _period()), (180, _period()))

        with patch.object(statement_check, "maintenance_overcharge",
                          side_effect=RuntimeError("bug")):
            (charge, _), _ = self._read(book, _row(book))

        self.assertEqual(charge, 360)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheInstanceKeepsRunningTests(unittest.TestCase):
    """The whole tick: a peer above its quote is scored, and the child is charged our
    price as before, and keeps running."""

    OWN_MU = 250

    def _tick(self, book, row):
        sc = MagicMock()
        sc.get_delegated_instances.return_value = [row]
        sc.get_delegated_row.side_effect = lambda token: dict(row)
        sc.statement_tick_due.return_value = False
        sc.peers_with_unsettled_starts.return_value = []
        sc.peers_with_client.return_value = []
        sc.child_statement_due.return_value = True
        with patch.object(maintain, "sc", sc), \
                patch.object(statement_check, "read", _fetch_of(book)), \
                patch.object(maintain, "_balance_held_on", return_value=10 ** 9), \
                patch.object(statement_check, "stored_rates", return_value={}), \
                patch.object(maintain, "get_client_id_on_other_peer", return_value="client-on-peer"), \
                patch.object(statement_check, "settle_failed_reads"), \
                patch.object(maintain, "charge_at_launch_price", return_value=self.OWN_MU), \
                patch.object(maintain, "stop_instance") as stop, \
                patch.object(maintain, "ALLOW_DEBT", False), \
                patch.object(statement_check, "penalise_once") as penalise:
            maintain.maintain_delegated_instances()
        return sc.settle_delegated_tick.call_args.kwargs, stop, penalise

    def test_an_overcharge_is_absorbed_and_the_client_pays_the_same(self):
        honest = _book((100, _period()), (100, _period()))
        greedy = _book((400, _period()), (400, _period()))

        charged, stop, penalise = self._tick(honest, _row(honest))
        self.assertEqual(charged["charge_mu"], self.OWN_MU)
        stop.assert_not_called()
        penalise.assert_not_called()

        charged_greedy, stop, penalise = self._tick(greedy, _row(greedy))
        self.assertEqual(charged_greedy["charge_mu"], self.OWN_MU)
        self.assertEqual(charged_greedy["movements"], charged["movements"])
        stop.assert_not_called()
        self.assertEqual([c.args[2] for c in penalise.call_args_list],
                         [Reason.MAINTENANCE_OVERCHARGED])
        # What the peer charged is measured, and is a loss of this node.
        self.assertEqual(charged_greedy["mark"]["charged_delta"], 800)


class _Database(unittest.TestCase):
    """An in-memory database with the real schema."""

    def setUp(self):
        import sqlite3

        from src.database.migrate import create_tables
        from src.database.sql_connection import SQLConnection

        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.execute(f"INSERT INTO peer (id) VALUES ('{PEER_ID}')")
        connection.commit()
        SQLConnection._connection = connection
        self.addCleanup(self._restore)

    def _restore(self):
        from src.database.sql_connection import SQLConnection
        SQLConnection._connection.close()
        SQLConnection._connection = self._original


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OnlyThePeerIsScoredTests(_Database):
    """The real penalty, on the real tables: the peer and nobody else, and once per
    window."""

    def setUp(self):
        super().setUp()
        peer = patch("src.reputation_system.interface.update_peer_reputation")
        vmachine = patch("src.reputation_system.interface.update_vmachine_reputation")
        self.peer_score = peer.start()
        self.service_score = vmachine.start()
        self.addCleanup(peer.stop)
        self.addCleanup(vmachine.stop)

    def _check(self, rows, account=PEER_TOKEN):
        row = dict(QUOTE, peer_id=PEER_ID, token=account, id=OUR_ALIAS)
        maintain._check_maintenance_quote(row, rows, 1000)

    def test_neither_the_service_nor_the_client_is_penalised(self):
        self._check(_rows((400, 10)))

        self.peer_score.assert_called_once_with(
            peer_id=PEER_ID, amount=statement_check.STATEMENT_PENALTY,
            reason=Reason.MAINTENANCE_OVERCHARGED)
        self.service_score.assert_not_called()
        events = self.sc._execute("SELECT * FROM statement_faults").fetchall()
        self.assertEqual([(e["peer_id"], e["reason"]) for e in events],
                         [(PEER_ID, Reason.MAINTENANCE_OVERCHARGED)])

    def test_a_long_overcharge_is_scored_once_in_the_window(self):
        for read in range(5):
            # Each read shows new movements.
            self._check(_rows((400, 10), start_ts=1000 + read * 10, first_seq=3 + read))

        self.peer_score.assert_called_once()

    def test_after_the_window_it_is_scored_again_but_a_movement_only_once(self):
        self._check(_rows((400, 10)))
        old = int(time.time()) - statement_check.FAULT_WINDOW_SECONDS - 1
        self.sc._execute("UPDATE statement_faults SET ts = ?, period = ?", (old, old))

        self._check(_rows((400, 10)))  # the same movement, read again
        self.assertEqual(self.peer_score.call_count, 1)

        self._check(_rows((400, 10), first_seq=53))
        self.assertEqual(self.peer_score.call_count, 2)

    def test_each_child_is_its_own_account(self):
        self._check(_rows((400, 10)), account=PEER_TOKEN)
        self._check(_rows((400, 10)), account="another-child")

        self.assertEqual(self.peer_score.call_count, 2)

    def test_the_other_faults_keep_their_rule(self):
        """A fault of another reason at a movement is scored once at each movement."""
        statement_check.penalise_once(PEER_ID, "acct", Reason.STATEMENT_OVERCHARGED, "x", 5)
        statement_check.penalise_once(PEER_ID, "acct", Reason.STATEMENT_OVERCHARGED, "y", 6)
        statement_check.penalise_once(PEER_ID, "acct", Reason.STATEMENT_OVERCHARGED, "y", 6)

        self.assertEqual(self.peer_score.call_count, 2)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheQuoteIsStoredUnconvertedTests(_Database):
    """The quote goes from the answer of the peer to the delegated row in the MU of the
    peer. The peer MU are worth half of ours here."""

    PAYMENT_SYSTEM = SimpleNamespace(local_mu_per_unit=1_000_000_000,
                                     peer_mu_per_unit=2_000_000_000)

    def _peer_quote(self):
        return celaut.EstimatedCost(
            cost=celaut.Amount(n="5001"), init_maintenance_cost=celaut.Amount(n="61"),
            max_maintenance_cost=celaut.Amount(n="101"), maintenance_seconds_loop=10,
            variance=0.25)

    # A peer of its own: the quotes of other tests stay in the query cache.
    ASKED_PEER = "peer-of-the-quote-test-567"

    def _ask(self, peer_quote):
        with patch.object(balancer_mod, "matching_payment_system",
                          return_value=self.PAYMENT_SYSTEM), \
                patch.object(balancer_mod, "peer_channel"), \
                patch.object(balancer_mod, "get_client_id_on_other_peer", return_value="c"), \
                patch.object(balancer_mod, "peer_quote_ttl", return_value=30), \
                patch.object(balancer_mod.BeeClient, "get_service_estimated_cost",
                             return_value=peer_quote) as asked:
            local = balancer_mod.estimate_cost_on_peer(
                peer_id=self.ASKED_PEER, resources=celaut.Service.Container.Resources(),
                metadata=celaut.Metadata(), configuration=celaut.Configuration())
        return local, asked

    def test_the_balancer_keeps_the_quote_as_the_peer_sent_it(self):
        quote = self._peer_quote()

        local, _ = self._ask(quote)

        # Our MU: half, rounded up. The quote of the peer is kept apart, as it came.
        self.assertEqual(local.max_maintenance_cost.n, "51")
        self.assertEqual(peer_quotes.signed(self.ASKED_PEER, local), quote)
        # From the cache too, with no question to the peer.
        peer_quotes._quotes.clear()
        again, asked = self._ask(quote)
        asked.assert_not_called()
        self.assertEqual(peer_quotes.signed(self.ASKED_PEER, again), quote)

    def test_a_quote_that_was_not_kept_is_none(self):
        self.assertIsNone(peer_quotes.signed("another-peer", self._peer_quote()))
        self.assertIsNone(peer_quotes.maintenance_terms(None))

    def test_the_kept_quotes_are_bounded(self):
        with patch.object(peer_quotes, "MAX_QUOTES", 3):
            for i in range(5):
                peer_quotes.remember(f"p{i}", self._peer_quote(), self._peer_quote())
            self.assertLessEqual(len(peer_quotes._quotes), 3)
            self.assertIsNone(peer_quotes.signed("p0", self._peer_quote()))
            self.assertIsNotNone(peer_quotes.signed("p4", self._peer_quote()))

    def test_the_launcher_hands_the_signed_quote_to_the_delegation(self):
        local = celaut.EstimatedCost(cost=celaut.Amount(n="1100"))
        signed = self._peer_quote()
        peer_quotes.remember(PEER_ID, local, signed)
        configuration = celaut.Configuration()
        configuration.initial_mu.n = "1000"
        with patch.object(launch_service_mod, "_own_price",
                          return_value=celaut.EstimatedCost(cost=configuration.initial_mu)), \
                patch.object(launch_service_mod.sc, "peer_exists", return_value=True), \
                patch.object(launch_service_mod, "service_requires_parent_colocation",
                             return_value=False), \
                patch.object(launch_service_mod, "estimate_cost_on_peer", return_value=local), \
                patch.object(launch_service_mod, "spend_mu", return_value=True), \
                patch.object(launch_service_mod, "delegate_execution",
                             return_value=celaut.ServiceInstance()) as delegate, \
                patch.object(launch_service_mod.sc, "internal_instance_exists",
                             return_value=False):
            launch_service_mod._force_delegate(
                forced_peer=PEER_ID, service=celaut.Service(), service_id="svc-1",
                metadata=celaut.Metadata(), configuration=configuration,
                father_id="dev-client-1", father_ip="10.0.0.1", recursion_guard_token="tok")

        self.assertEqual(delegate.call_args.kwargs["peer_quote"], signed)

    def test_the_delegation_writes_the_quote_in_the_mu_of_the_peer(self):
        config = celaut.Configuration()
        config.initial_mu.n = "10000"
        with patch.object(delegate_mod, "matching_payment_system",
                          return_value=self.PAYMENT_SYSTEM), \
                patch.object(delegate_mod, "balance_on_other_peer", return_value=10 ** 12), \
                patch.object(delegate_mod, "get_client_id_on_other_peer", return_value="c"), \
                patch.object(delegate_mod, "peer_channel"), \
                patch.object(delegate_mod.utils, "service_extended"), \
                patch.object(delegate_mod.BeeClient, "start_service",
                             return_value=celaut.ServiceInstance(token=PEER_TOKEN)), \
                patch.object(delegate_mod.delegated_endpoints, "should_tunnel",
                             return_value=False), \
                patch.object(delegate_mod, "_check_start", return_value=(19_000, None)):
            delegate_mod.delegate_execution(
                service_id="service-a", peer=PEER_ID, father_id=FATHER,
                metadata=celaut.Metadata(), config=config, recursion_guard_token="t",
                refund_container=[lambda: None], peer_quote=self._peer_quote())

        row = self.sc.get_delegated_row(PEER_TOKEN)
        self.assertEqual(
            {k: row[k] for k in QUOTE},
            {"quote_init_maintenance_cost": 61, "quote_max_maintenance_cost": 101,
             "quote_maintenance_seconds_loop": 10, "quote_variance": 0.25})
        self.assertEqual(MaintenanceQuote.of_row(row), MaintenanceQuote(101, 10, 0.25))

    def test_a_delegation_with_no_quote_writes_none(self):
        self.sc.add_delegated_instance(
            father_id=FATHER, alias=OUR_ALIAS, external_token=PEER_TOKEN, peer_id=PEER_ID,
            serialized_instance=b"", service_id="s", balance_mu=900, peer_balance_mu=1800)

        row = self.sc.get_delegated_row(PEER_TOKEN)
        self.assertEqual({row[k] for k in QUOTE}, {None})
        self.assertIsNone(MaintenanceQuote.of_row(row))


if __name__ == "__main__":
    unittest.main()

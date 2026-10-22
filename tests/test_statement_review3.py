"""The points of the third review of #570, each against a real database and a fake peer.

The peer keeps our account as a node does (``SQLConnection.balance_statement``): it
charges each read in the account, in the same transaction as the read, and the charge
names the range of the read. The network is the only part that is replaced. Each test
shows a problem that the review found, and that the fix removes.
"""
import json
import sqlite3
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()
    import grpc

    from protos import celaut_pb2
    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.gateway.launcher.delegate_execution import delegate_execution as delegate_mod
    from src.manager import balance_statement, maintain, manager, statement_check
    from src.manager import metrics as metrics_mod
    from src.manager.manager import delegation_lock
    from src.manager.statement_check import Row
    from tests.statement_chain import FakeLedger
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

PEER = "peer"
CLIENT = "our-client-on-the-peer"
TOKEN = "token-of-the-child-on-the-peer"
ALIAS = "alias-of-the-child"
FATHER = "father-client"
DAY = 86400


def coded(code):
    class Coded(grpc.RpcError):
        def code(self):
            return code

        def __str__(self):
            return code.name

    return Coded()


def detail(**values) -> str:
    return json.dumps({key: str(value) for key, value in values.items()},
                      sort_keys=True, separators=(",", ":"))


class FakePeer:
    """Our account on a peer. ``clock`` is how far the clock of the peer is from ours.
    ``refuse``: answer with this error, and charge nothing. ``lose``: charge the read,
    and lose the answer with this error. ``claim``: the rows that a charge says."""

    def __init__(self, rates):
        self.book = FakeLedger(CLIENT, start_ts=int(time.time()) - 3600)
        self.rates = rates
        self.clock = 0
        self.refuse = None
        self.lose = None
        self.claim = None

    def now(self) -> int:
        return int(time.time()) + self.clock

    def add(self, amount, kind, ref="", text="", ts=None) -> dict:
        return self.book.add(amount, kind, ref, text, self.now() if ts is None else ts)

    def fetch(self, peer_id, client_id, token="", from_seq=0, to_seq=0, **_):
        if self.refuse is not None:
            raise self.refuse
        rows = [] if from_seq <= 0 else [
            row for row in self.book.rows
            if row["seq"] >= from_seq and (to_seq <= 0 or row["seq"] <= to_seq)][:1000]
        shown = len(rows) if self.claim is None else self.claim
        price = (self.rates["statement_query_mu"]
                 + self.rates["statement_row_mu"] * (shown + 1))
        self.add(-price, ledger.STATEMENT_QUERY,
                 text=detail(from_seq=from_seq, rows=shown, to_seq=to_seq))
        if self.lose is not None:
            raise self.lose
        return self.book.statement(from_seq=from_seq, to_seq=to_seq, ts=self.now() + 1)


class _Node(unittest.TestCase):
    """This node, with an in-memory database, a client on ``PEER`` and that peer."""

    RATES = {"statement_query_mu": 100, "statement_row_mu": 1}

    def setUp(self):
        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.execute("INSERT INTO peer (id) VALUES (?)", (PEER,))
        connection.commit()
        SQLConnection._connection = connection
        self.addCleanup(self._restore)
        self.sc.add_external_client(PEER, CLIENT)

        scores = patch("src.reputation_system.interface.update_peer_reputation")
        self.scores = scores.start()
        self.addCleanup(scores.stop)
        self.peer = FakePeer(self.RATES)
        self.metrics = None

    def _restore(self):
        SQLConnection._connection.close()
        SQLConnection._connection = self._original

    def balance(self, peer_id=PEER):
        return self.peer.book.balance if self.metrics is None else self.metrics

    def network(self):
        """The calls to the peer, answered by ``self.peer``."""
        stack = [
            patch.object(balance_statement, "fetch", side_effect=self.peer.fetch),
            patch.object(maintain, "_balance_held_on", side_effect=self.balance),
            patch.object(metrics_mod, "refresh_balance_on_peer", side_effect=self.balance),
            patch.object(delegate_mod, "refresh_balance_on_peer", side_effect=self.balance),
            patch.object(statement_check, "stored_rates", return_value=self.RATES),
            patch.object(maintain, "_peer_rates", return_value=self.RATES),
        ]
        for item in stack:
            item.start()
        return stack

    def check(self):
        """One read of our account on the peer, as the tick does it."""
        stack = self.network()
        try:
            maintain._check_our_account(PEER)
        finally:
            for item in reversed(stack):
                item.stop()

    @property
    def score_count(self):
        return self.scores.call_count

    def reasons(self):
        return [c.kwargs["reason"] for c in self.scores.call_args_list]

    def start_child(self, attempt, amount, token, ts=None):
        """The peer charges a start and names its child."""
        start = self.peer.add(-amount, ledger.CHILD_START, ts=ts)
        self.peer.add(0, ledger.CHILD_BOUND, ledger.instance_ref(token),
                      detail(starts_seq=start["seq"]), ts=ts)
        self.sc.bind_start_attempt(attempt, token)
        return start

    def add_child(self):
        """A delegated child on the peer, of a father with a balance here."""
        self.sc.add_client(FATHER, 100_000, None)
        self.sc.add_delegated_instance(
            father_id=FATHER, encrypted_external_token=ALIAS, external_token=TOKEN,
            peer_id=PEER, serialized_instance=b"", service_id="s", balance_mu=900,
            peer_balance_mu=900, movements=(ledger.Movement(ledger.START, 900),),
            sent_peer_mu=1000, mark=None,
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RetryOfAnotherCandidateTests(_Node):
    """New problem 1: the peer retries another candidate while our read lands."""

    def setUp(self):
        super().setUp()
        self.peer.add(100_000, ledger.CREDIT)
        self.attempt = self.sc.add_start_attempt(PEER, 1000)
        self.first = self.peer.add(-1000, ledger.CHILD_START)
        self.check()  # during the build of the first candidate

    def fail_first_candidate(self):
        self.peer.add(1000, ledger.REFUND, text=detail(refunds_seq=self.first["seq"]))

    def test_a_read_during_the_first_candidate_and_one_after_the_bind_score_nothing(self):
        self.fail_first_candidate()
        self.start_child(self.attempt, 1000, TOKEN)

        self.check()

        self.assertEqual(self.score_count, 0, self.reasons())

    def test_a_read_during_each_candidate_scores_nothing(self):
        self.fail_first_candidate()
        second = self.peer.add(-1000, ledger.CHILD_START)
        self.check()  # during the build of the second candidate

        self.peer.add(0, ledger.CHILD_BOUND, ledger.instance_ref(TOKEN),
                      detail(starts_seq=second["seq"]))
        self.sc.bind_start_attempt(self.attempt, TOKEN)
        self.check()

        self.assertEqual(self.score_count, 0, self.reasons())

    def test_a_start_paid_twice_is_still_a_fault(self):
        self.fail_first_candidate()
        self.start_child(self.attempt, 1000, TOKEN)
        self.check()
        self.start_child(self.attempt, 1000, TOKEN)

        self.check()

        self.assertEqual(self.reasons(), ["statement_overcharged"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConcurrentStartsTests(_Node):
    """New problem 5: two starts at the same time on the same peer."""

    def setUp(self):
        super().setUp()
        self.peer.add(100_000, ledger.CREDIT)

    def _two_starts(self, sent_a, sent_b):
        a = self.sc.add_start_attempt(PEER, sent_a)
        b = self.sc.add_start_attempt(PEER, sent_b)
        # The peer charges B first, and our read lands before either child is named.
        start_b = self.peer.add(-sent_b, ledger.CHILD_START)
        self.check()
        start_a = self.peer.add(-sent_a, ledger.CHILD_START)
        self.peer.add(0, ledger.CHILD_BOUND, ledger.instance_ref("token-b"),
                      detail(starts_seq=start_b["seq"]))
        self.peer.add(0, ledger.CHILD_BOUND, ledger.instance_ref("token-a"),
                      detail(starts_seq=start_a["seq"]))
        self.sc.bind_start_attempt(a, "token-a")
        self.sc.bind_start_attempt(b, "token-b")
        self.check()

    def test_a_smaller_start_charged_first_is_not_paid_twice(self):
        self._two_starts(1000, 100)

        self.assertEqual(self.score_count, 0, self.reasons())

    def test_two_starts_of_the_same_amount_change_places(self):
        self._two_starts(1000, 1000)

        self.assertEqual(self.score_count, 0, self.reasons())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RefusedStartTests(_Node):
    """New problem 6, and point 1: a refused ``StartService`` leaves no open attempt."""

    PAYMENT_SYSTEM = SimpleNamespace(local_mu_per_unit=1_000_000_000,
                                     peer_mu_per_unit=1_000_000_000)

    def setUp(self):
        super().setUp()
        self.peer.add(100_000, ledger.CREDIT)
        self.check()

    def delegate(self, during=None):
        """Ask the peer for a start of 50 000 MU, which it refuses. ``during`` runs while
        the peer works on it."""
        config = celaut_pb2.Configuration()
        config.initial_mu.n = "50000"

        def start_service(*args, **kwargs):
            if during:
                during()
            raise coded(grpc.StatusCode.FAILED_PRECONDITION)

        with patch.object(delegate_mod, "matching_payment_system",
                          return_value=self.PAYMENT_SYSTEM), \
                patch.object(delegate_mod, "balance_on_other_peer", return_value=10 ** 12), \
                patch.object(delegate_mod, "get_client_id_on_other_peer", return_value=CLIENT), \
                patch.object(delegate_mod, "peer_channel"), \
                patch.object(delegate_mod.utils, "service_extended", return_value=MagicMock()), \
                patch.object(delegate_mod.BeeClient, "start_service", side_effect=start_service):
            with self.assertRaises(Exception):
                delegate_mod.delegate_execution(
                    service_id="s", peer=PEER, father_id=FATHER,
                    metadata=celaut_pb2.Metadata(), config=config,
                    recursion_guard_token="guard", refund_container=[lambda: True])

    def test_a_start_that_the_peer_charges_after_it_refused_is_a_fault(self):
        self.delegate()
        self.peer.add(-50_000, ledger.CHILD_START)

        self.check()

        self.assertEqual(self.reasons(), ["statement_overcharged"])

    def test_a_candidate_that_the_peer_gave_back_before_it_refused_is_fine(self):
        def candidate():
            start = self.peer.add(-50_000, ledger.CHILD_START)
            self.check()  # our read lands during the build
            self.peer.add(50_000, ledger.REFUND, text=detail(refunds_seq=start["seq"]))

        self.delegate(during=candidate)
        self.check()

        self.assertEqual(self.score_count, 0, self.reasons())

    def test_a_candidate_that_the_peer_keeps_after_it_refused_is_a_fault(self):
        self.delegate(during=lambda: (self.peer.add(-50_000, ledger.CHILD_START), self.check()))

        self.check()

        self.assertEqual(self.reasons(), ["statement_overcharged"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TopUpReservationTests(_Node):
    """New problem 7, and point 1: the reservation of a top-up."""

    PAYMENT_SYSTEM = SimpleNamespace(local_mu_per_unit=1_000_000_000,
                                     peer_mu_per_unit=1_000_000_000)

    def setUp(self):
        super().setUp()
        self.add_child()
        self.peer.add(100_000, ledger.CREDIT)
        self.check()

    def modify(self, amount, answer):
        """``modify_deposit`` of ``amount`` MU; ``answer(**call)`` is the peer."""
        with patch.object(manager, "matching_payment_system", return_value=self.PAYMENT_SYSTEM), \
                patch.object(manager, "peer_channel"), \
                patch.object(manager.BeeClient, "call_one", side_effect=answer):
            return manager.modify_deposit(amount_mu=amount, service_token=ALIAS)

    def father(self):
        return self.sc.get_client_balance(FATHER)[0]

    def test_an_empty_answer_is_a_failure_and_the_father_gets_his_mu_back(self):
        ok, _ = self.modify(300, lambda **call: None)

        self.assertFalse(ok)
        self.assertEqual(self.father(), 100_000)

    def test_a_top_up_long_after_its_reservation_is_a_fault(self):
        self.modify(300, lambda **call: None)
        later = int(time.time()) + 20 * DAY

        with patch("time.time", return_value=later):
            self.peer.add(-300, ledger.TOP_UP, ledger.instance_ref(TOKEN))
            self.check()

        self.assertEqual(self.reasons(), ["statement_overcharged"])

    def test_a_top_up_whose_answer_was_lost_is_not_a_fault(self):
        def applied_and_lost(**call):
            self.peer.add(-300, ledger.TOP_UP, ledger.instance_ref(TOKEN))
            raise coded(grpc.StatusCode.UNAVAILABLE)

        ok, _ = self.modify(300, applied_and_lost)
        self.check()

        self.assertFalse(ok)
        self.assertEqual(self.score_count, 0, self.reasons())

    def test_a_top_up_that_the_peer_refused_takes_nothing(self):
        self.modify(300, lambda **call: celaut_pb2.ModifyDepositOutput(success=False))
        self.peer.add(-300, ledger.TOP_UP, ledger.instance_ref(TOKEN))

        self.check()

        self.assertEqual(self.reasons(), ["statement_overcharged"])

    def test_the_call_to_the_peer_has_a_time_limit(self):
        calls = []

        def answer(**call):
            calls.append(call)
            self.peer.add(-300, ledger.TOP_UP, ledger.instance_ref(TOKEN))
            return celaut_pb2.ModifyDepositOutput(success=True)

        ok, _ = self.modify(300, answer)

        self.assertTrue(ok)
        self.assertTrue(calls[0].get("timeout"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DelegationLockTests(_Node):
    """New problem 2: a ``ModifyDeposit`` that the peer does not answer."""

    def test_the_tick_does_not_wait_for_a_deposit_that_is_changing(self):
        self.add_child()
        lock = delegation_lock(ALIAS)
        lock.acquire()
        read = MagicMock(side_effect=RuntimeError("not reached"))
        try:
            with patch.object(maintain, "_check_our_account"), \
                    patch.object(maintain, "_read_child", read), \
                    patch.object(maintain, "is_peer_available", return_value=False):
                tick = threading.Thread(target=maintain.maintain_delegated_instances, daemon=True)
                tick.start()
                tick.join(5)
                waited = tick.is_alive()
        finally:
            lock.release()
            tick.join(10)

        self.assertFalse(waited, "the tick waited for the lock of the deposit")
        read.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerChosenUnavailableTests(_Node):
    """New problem 3, and point 3: ``UNAVAILABLE`` is a code that the peer chooses."""

    def setUp(self):
        super().setUp()
        self.peer.add(100_000, ledger.CREDIT)
        self.check()

    def age_mark(self, seconds):
        SQLConnection._connection.execute(
            "UPDATE peer SET statement_ts = statement_ts - ? WHERE id = ?", (seconds, PEER))
        SQLConnection._connection.commit()

    def test_a_peer_that_answers_metrics_and_no_statement_for_days_is_scored(self):
        self.age_mark(4 * DAY)
        self.peer.refuse = coded(grpc.StatusCode.UNAVAILABLE)

        for _ in range(3):
            self.check()

        self.assertEqual(self.reasons(), ["statement_inconsistent"])

    def test_a_short_outage_is_not_scored(self):
        self.peer.refuse = coded(grpc.StatusCode.UNAVAILABLE)

        self.check()

        self.assertEqual(self.score_count, 0)

    def test_an_account_with_no_child_is_not_read_at_every_tick(self):
        self.age_mark(2 * DAY)
        self.peer.refuse = coded(grpc.StatusCode.UNAVAILABLE)
        before = self.sc.get_statement_counters(PEER)

        stack = self.network()
        try:
            for _ in range(10):
                maintain.maintain_delegated_instances()
        finally:
            for item in reversed(stack):
                item.stop()

        after = self.sc.get_statement_counters(PEER)
        self.assertLessEqual(after["reads"] - before["reads"], 1)
        self.assertLessEqual(after["rows_priced"] - before["rows_priced"], 10)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FailedReadPriceTests(_Node):
    """New problem 4, and point 8: what a read that failed may be charged."""

    def setUp(self):
        super().setUp()
        self.peer.add(100_000, ledger.CREDIT)
        self.check()

    def test_rows_written_after_a_failed_read_and_dated_back_do_not_price_it(self):
        self.peer.lose = coded(grpc.StatusCode.DEADLINE_EXCEEDED)
        self.peer.claim = 300  # the charge says 300 rows, and the range holds none
        self.check()
        self.peer.lose = self.peer.claim = None
        failed_at = self.peer.now()
        for _ in range(300):
            self.peer.add(0, ledger.CREDIT, ts=failed_at)

        self.check()

        self.assertEqual(self.reasons(), ["statement_overcharged"])

    def test_an_honest_charge_of_a_failed_read_is_not_a_fault(self):
        for _ in range(5):
            self.peer.add(1, ledger.CREDIT)
        self.peer.lose = coded(grpc.StatusCode.DEADLINE_EXCEEDED)
        self.check()
        self.peer.lose = None
        for _ in range(300):
            self.peer.add(0, ledger.CREDIT)

        self.check()

        self.assertEqual(self.score_count, 0, self.reasons())

    def test_a_failed_read_of_a_child_is_priced_when_pages_show_its_range(self):
        with patch.object(balance_statement, "fetch",
                          side_effect=coded(grpc.StatusCode.DEADLINE_EXCEEDED)):
            with self.assertRaises(grpc.RpcError):
                statement_check.read(PEER, CLIENT, token=TOKEN, from_seq=1, rows=1000)
        asked_at = int(time.time())
        before = self.sc.get_statement_counters(PEER)["rows_priced"]
        # The child held 10 movements when the read was asked, and 990 came later.
        rows = [Row(seq, asked_at - 100 if seq <= 10 else asked_at + 3600, -1, 0, "maintenance", "")
                for seq in range(1, 1001)]
        for page in (rows[:399], rows[399:798], rows[798:]):
            statement_check.settle_failed_reads(PEER, TOKEN, page, 1000)

        after = self.sc.get_statement_counters(PEER)["rows_priced"]
        self.assertEqual(before - after, 1000 - 10)
        self.assertEqual(self.sc.failed_statement_reads(PEER, ledger.account_ref(TOKEN)), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ClockTests(_Node):
    """New problem 8: a step in a clock."""

    def setUp(self):
        super().setUp()
        self.peer.add(100_000, ledger.CREDIT)
        self.check()

    def test_a_step_back_of_the_clock_of_the_peer_between_two_reads_scores_nothing(self):
        self.peer.clock = -600
        self.peer.add(1, ledger.CREDIT)
        self.peer.add(1, ledger.CREDIT)
        self.peer.clock = 0

        self.check()

        self.assertEqual(self.score_count, 0, self.reasons())

    def test_a_clock_that_is_off_is_scored_once_and_not_at_every_read(self):
        self.peer.clock = 300

        for _ in range(3):
            self.check()

        self.assertEqual(self.reasons(), ["statement_inconsistent"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LowMetricsTests(_Node):
    """Points 9 and 10: ``Metrics`` is free and not signed."""

    RATES = {"statement_query_mu": 10, "statement_row_mu": 1}

    def setUp(self):
        super().setUp()
        self.peer.add(1_000_000, ledger.CREDIT)
        self.check()
        self.peer.add(-900_000, ledger.DEBIT)

    def test_a_metrics_balance_that_pays_for_nothing_does_not_stop_the_check(self):
        for metrics in (0, 9):
            self.metrics = metrics
            self.check()

        self.assertEqual(self.reasons(), ["statement_overcharged"])

    def test_a_refusal_for_lack_of_balance_that_the_signed_balance_pays_for_is_a_fault(self):
        self.metrics = 10
        self.peer.refuse = coded(grpc.StatusCode.RESOURCE_EXHAUSTED)

        self.check()

        self.assertEqual(self.reasons(), ["statement_inconsistent"])

    def test_the_statement_of_a_new_child_refused_with_metrics_at_0_is_a_fault(self):
        self.peer.refuse = coded(grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.metrics = 0
        stack = self.network()
        try:
            with patch.object(delegate_mod, "_child_balance_on_peer", return_value=0):
                delegate_mod._check_start(PEER, CLIENT, TOKEN, started_at=time.time())
        finally:
            for item in reversed(stack):
                item.stop()

        self.assertEqual(self.reasons(), ["statement_inconsistent"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RefusalWindowTests(_Node):
    """Point 12: a refusal is scored once in 24 hours, not once in a day of UTC."""

    def refuse_at(self, when):
        with patch("time.time", return_value=when):
            statement_check.penalise_no_statement(PEER, CLIENT, RuntimeError("refused"), seq=3)

    def test_two_refusals_on_each_side_of_midnight_utc_are_scored_once(self):
        midnight = (int(time.time()) // DAY + 1) * DAY
        self.refuse_at(midnight - 60)
        self.refuse_at(midnight + 60)
        self.assertEqual(self.score_count, 1)

        self.refuse_at(midnight - 60 + DAY)
        self.assertEqual(self.score_count, 2)


if __name__ == "__main__":
    unittest.main()

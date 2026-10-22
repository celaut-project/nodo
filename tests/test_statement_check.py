"""Checking a peer's statements (#570): that they add up, and that each charge follows
its rule. A peer that fails either loses reputation; nothing else may."""

import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()
    import grpc

    from protos import celaut_pb2
    from src.database import ledger
    from src.manager import statement_check as check
    from src.manager.statement_check import Mark, Row
    from tests.statement_chain import FakeLedger
    from src.utils.monetary import GIB
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def chained(*movements, account="acct"):
    """A ledger from (amount, kind[, ref, detail, ts]) tuples, as a node builds it."""
    out = FakeLedger(account)
    for movement in movements:
        amount, kind = movement[:2]
        out.add(amount, kind, *movement[2:5])
    return out


def verify(book, mark=None, *, from_seq=None, now=None, **statement_args):
    """``verify_chain`` of what ``book`` shows from the row after ``mark``."""
    mark = check.NO_MARK if mark is None else mark
    from_seq = (1 if mark.seq is None else mark.seq + 1) if from_seq is None else from_seq
    statement = book.statement(from_seq=from_seq, **statement_args)
    return check.verify_chain(
        statement, mark, now=statement.ts if now is None else now, from_seq=from_seq
    ), statement


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChainTests(unittest.TestCase):

    def test_a_new_account_adds_up_from_its_genesis(self):
        book = chained((100, "start"), (-30, "build"))

        chain, _ = verify(book, Mark.new_account("acct", ts=0))

        self.assertIsNone(chain.problem)
        self.assertEqual([r.seq for r in chain.rows], [1, 2])
        self.assertFalse(chain.more)

    def test_a_first_read_takes_what_the_node_holds_as_its_start(self):
        """The movements that the retention deleted before the first read are accepted."""
        book = chained((100, "start"), (-30, "build"), (-5, "maintenance"), (-5, "maintenance"))
        book.purge_before(3)

        chain, _ = verify(book)

        self.assertIsNone(chain.problem)
        self.assertEqual([r.seq for r in chain.rows], [3, 4])
        self.assertTrue(chain.gap)

    def test_the_movements_after_the_mark_are_new(self):
        book = chained((100, "start"), (-30, "build"), (-5, "maintenance"))

        chain, _ = verify(book, book.mark_at(2))

        self.assertIsNone(chain.problem)
        self.assertEqual([r.seq for r in chain.rows], [3])
        self.assertFalse(chain.gap)

    def test_a_node_that_is_caught_up_shows_nothing_new(self):
        book = chained((100, "start"), (-30, "build"))

        chain, _ = verify(book, book.mark_at(2))

        self.assertEqual(chain.rows, [])
        self.assertFalse(chain.more)

    def test_a_mark_that_changed_does_not_hang(self):
        """The node rewrote a movement that it had shown: the hash of the mark differs."""
        shown = chained((100, "start"), (-30, "build"))
        mark = shown.mark_at(2)
        rewritten = chained((100, "start"), (-31, "build"), (-5, "maintenance"))

        chain, _ = verify(rewritten, mark)

        self.assertIsNone(chain.rows)
        self.assertIn("is not the movement that it was", chain.problem)

    def test_a_node_that_is_caught_up_must_show_the_head_it_showed(self):
        shown = chained((100, "start"), (-30, "build"))
        mark = shown.mark_at(2)
        rewritten = chained((100, "start"), (-31, "build"))

        chain, _ = verify(rewritten, mark)

        self.assertIsNone(chain.rows)

    def test_a_gap_that_the_retention_does_not_excuse_is_a_fault(self):
        """The movements after a recent mark cannot have been deleted yet."""
        book = chained((100, "start"), (-30, "build"), (-5, "maintenance"), (-5, "maintenance"))
        mark = book.mark_at(1)
        book.purge_before(3)

        chain, _ = verify(book, mark)

        self.assertIsNone(chain.rows)
        self.assertIn("were deleted before", chain.problem)

    def test_a_new_account_excuses_nothing_that_a_node_hides(self):
        """The mark of an account that was just created is as young as the account, so a
        first read cannot excuse a movement that the node does not show."""
        book = chained((100, "start"), (-900000, "debit"), (-5, "maintenance"))
        statement_ts = book.statement(from_seq=1).ts
        mark = Mark.new_account("acct", ts=statement_ts - 60)
        book.purge_before(3)

        chain, _ = verify(book, mark)

        self.assertIsNone(chain.rows)
        self.assertIn("were deleted before", chain.problem)

    def test_a_gap_after_an_old_mark_is_the_retention(self):
        book = chained((100, "start"), (-30, "build"), (-5, "maintenance"), (-5, "maintenance"))
        mark = book.mark_at(1)
        book.purge_before(3)

        # The same read, 40 days after the mark: the movement after it may be deleted.
        later = book.statement(from_seq=2, ts=mark.ts + 40 * 86400)
        chain = check.verify_chain(later, mark, now=later.ts, from_seq=2)

        self.assertIsNone(chain.problem)
        self.assertTrue(chain.gap)
        self.assertEqual([r.seq for r in chain.rows], [3, 4])

    def test_a_node_cannot_excuse_a_gap_with_a_retention_of_zero(self):
        book = chained((100, "start"), (-30, "build"), (-5, "maintenance"), (-5, "maintenance"))
        mark = book.mark_at(1)
        book.purge_before(3)

        chain, _ = verify(book, mark, retention_days=0)

        self.assertIsNone(chain.rows)

    def test_a_node_that_holds_movements_and_does_not_show_them_is_a_fault(self):
        book = chained((100, "start"), (-30, "build"), (-5, "maintenance"))
        statement = book.statement(from_seq=1)
        statement.movements.pop(0)

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)

        self.assertIsNone(chain.rows)
        self.assertIn("are held and not shown", chain.problem)

    def test_a_gap_inside_the_statement_is_a_fault(self):
        book = chained((100, "start"), (-30, "build"), (-5, "maintenance"))
        statement = book.statement(from_seq=1)
        statement.movements.pop(1)

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)

        self.assertIn("missing", chain.problem)

    def test_a_movement_that_does_not_add_up_is_a_fault(self):
        book = chained((100, "start"), (-30, "build"))
        statement = book.statement(from_seq=1)
        statement.movements[1].balance_after.n = "71"

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)

        self.assertIsNone(chain.rows)

    def test_a_movement_that_does_not_hang_from_the_one_before_is_a_fault(self):
        book = chained((100, "start"), (-30, "build"))
        statement = book.statement(from_seq=1)
        statement.movements[1].prev_hash = "0" * 64

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)

        self.assertIn("does not hang", chain.problem)

    def test_the_first_movement_hangs_from_the_genesis_of_its_account(self):
        book = chained((100, "start"))
        statement = book.statement(from_seq=1)
        statement.movements[0].prev_hash = ledger.genesis_hash("another-account")

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)

        self.assertIn("genesis", chain.problem)

    def test_a_head_that_is_not_the_last_movement_is_a_fault(self):
        book = chained((100, "start"), (-30, "build"))
        statement = book.statement(from_seq=1)
        statement.head_hash = "f" * 64

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)

        self.assertIsNone(chain.rows)

    def test_a_balance_that_is_not_the_last_movement_is_a_fault(self):
        book = chained((100, "start"))
        statement = book.statement(from_seq=1)
        statement.balance.n = "99"

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)

        self.assertIsNotNone(chain.problem)

    def test_a_head_that_went_back_is_a_fault(self):
        book = chained((100, "start"), (-30, "build"))
        mark = book.mark_at(2)
        shorter = chained((100, "start"))

        chain, _ = verify(shorter, mark)

        self.assertIn("went back", chain.problem)

    def test_a_statement_dated_far_from_our_clock_is_a_fault(self):
        book = chained((100, "start"))

        chain, statement = verify(book, ts=10 ** 9, now=1000)

        self.assertIsNone(chain.rows)
        self.assertIn("is dated", chain.problem)

    def test_a_movement_dated_after_the_statement_is_a_fault(self):
        book = chained((100, "start"))
        statement = book.statement(from_seq=1)
        statement.movements[0].ts = statement.ts + 5

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)

        self.assertIn("after the statement", chain.problem)

    def test_an_amount_that_is_not_canonical_is_a_fault(self):
        book = chained((100, "start"))
        statement = book.statement(from_seq=1)
        statement.movements[0].amount.n = "0100"

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)

        self.assertIn("canonical", chain.problem)

    def test_an_account_with_no_movement_holds_nothing(self):
        empty = chained()
        statement = empty.statement(from_seq=1)

        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)
        self.assertEqual(chain.rows, [])

        statement.balance.n = "5"
        chain = check.verify_chain(statement, check.NO_MARK, now=statement.ts, from_seq=1)
        self.assertIsNone(chain.rows)

    def test_a_page_that_stops_short_says_there_is_more(self):
        book = chained(*[(1, "deposit")] * 5)

        chain, statement = verify(book, limit=2)

        self.assertEqual([r.seq for r in chain.rows], [1, 2])
        self.assertTrue(chain.more)
        # The next page goes on from the mark that the first one left.
        mark = check.mark_after(chain, statement, check.NO_MARK)
        chain, statement = verify(book, mark, limit=2)
        self.assertEqual([r.seq for r in chain.rows], [3, 4])
        self.assertTrue(chain.more)

    def test_the_mark_after_the_head_is_dated_by_the_statement(self):
        book = chained((100, "start"), (-30, "build"))

        chain, statement = verify(book)
        mark = check.mark_after(chain, statement, check.NO_MARK)

        self.assertEqual((mark.seq, mark.balance, mark.ts), (2, 70, statement.ts))

    def test_a_mark_at_the_head_of_a_statement_is_where_to_go_on_from(self):
        book = chained((100, "start"), (-30, "build"))
        statement = book.statement(from_seq=1)

        mark = check.anchor_at_head(statement)

        self.assertEqual((mark.seq, mark.hash, mark.balance), (2, book.rows[-1]["hash"], 70))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConsumptionTests(unittest.TestCase):
    """What a child consumed is what its movements say, less what its father moved."""

    def test_consumption_leaves_out_what_the_father_moved(self):
        rows = [Row(4, 0, -10, 0, ledger.MAINTENANCE, ""),
                Row(5, 0, 500, 0, ledger.TOP_UP, ""),
                Row(6, 0, -200, 0, ledger.WITHDRAWAL, ""),
                Row(7, 0, -20, 0, ledger.CHILD_START, "grandchild")]
        self.assertEqual(check.consumption(rows), 30)

    def test_what_the_child_does_with_its_own_balance_is_consumption_too(self):
        """Its tunnels, its resizes and its statements are spent from its balance."""
        rows = [Row(4, 0, -50, 0, kind, "") for kind in (
            ledger.TUNNEL, ledger.RESOURCE_CHANGE, ledger.STATEMENT_QUERY)]

        self.assertEqual(check.consumption(rows), 150)

    def test_consumption_is_never_below_zero(self):
        self.assertEqual(check.consumption([Row(4, 0, 80, 0, ledger.REFUND, "")]), 0)

    def test_the_net_of_the_father_is_signed_as_the_child_sees_it(self):
        rows = [Row(4, 0, -10, 0, ledger.MAINTENANCE, ""),
                Row(5, 0, 500, 0, ledger.TOP_UP, ""),
                Row(6, 0, -200, 0, ledger.WITHDRAWAL, ""),
                Row(7, 0, -20, 0, ledger.TOP_UP, "grandchild")]

        # The top-up of a child to its own child is negative on the child: not its father's.
        self.assertEqual(check.father_net(rows), 300)


def _check_account(rows, starts=None, attempts=(), allowances=None, now=10 ** 6):
    return check.account_faults(
        rows, {"child": 100} if starts is None else starts, attempts,
        {"child": 1000} if allowances is None else allowances, now=now)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AccountRuleTests(unittest.TestCase):

    @staticmethod
    def bound(seq, child="child", starts_seq=None):
        return Row(seq, 0, 0, 0, ledger.CHILD_BOUND, child,
                   {"starts_seq": str(seq - 1 if starts_seq is None else starts_seq)})

    def test_our_starts_top_ups_queries_and_credits_are_fine(self):
        rows = [Row(2, 0, 500, 0, ledger.DEPOSIT, ""),
                Row(3, 0, -100, 0, ledger.CHILD_START, ""),
                self.bound(4),
                Row(5, 0, -50, 0, ledger.TOP_UP, "child"),
                Row(6, 0, -1, 0, ledger.STATEMENT_QUERY, "child"),
                Row(7, 0, 20, 0, ledger.REFUND, "child")]

        result = _check_account(rows)

        self.assertEqual(result.faults, [])
        self.assertEqual(result.top_ups_taken, {"child": 50})

    def test_a_start_above_what_was_sent_is_an_overcharge(self):
        rows = [Row(3, 0, -101, 0, ledger.CHILD_START, ""), self.bound(4)]
        self.assertEqual(len(_check_account(rows).faults), 1)

    def test_a_start_that_was_given_back_is_fine(self):
        rows = [Row(3, 0, -100, 0, ledger.CHILD_START, ""),
                Row(4, 0, 100, 0, ledger.REFUND, "", {"refunds_seq": "3"})]
        self.assertEqual(_check_account(rows).faults, [])

    def test_a_charge_this_node_did_not_cause_is_an_overcharge(self):
        rows = [Row(3, 0, -5, 0, ledger.DEBIT, ""),
                Row(4, 0, -5, 0, ledger.TOP_UP, "not-ours"),
                Row(5, 0, -5, 0, ledger.MAINTENANCE, "")]
        self.assertEqual(len(_check_account(rows).faults), 3)

    def test_each_problem_comes_with_the_seq_where_it_shows(self):
        rows = [Row(3, 0, -5, 0, ledger.DEBIT, ""), Row(4, 0, -5, 0, ledger.MAINTENANCE, "")]

        self.assertEqual([seq for seq, _ in _check_account(rows).faults], [3, 4])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StartAttemptTests(unittest.TestCase):
    """A ``child_start`` that no child of ours explains is judged against what this node
    asked for, and nothing else: nothing waits, and nothing is excused for its age."""

    def test_a_start_that_nobody_asked_for_is_a_fault(self):
        for child_row in ([], [Row(4, 0, 0, 0, ledger.CHILD_BOUND, "stranger", {"starts_seq": "3"})]):
            rows = [Row(3, 1000, -900000, 0, ledger.CHILD_START, "")] + child_row

            faults = _check_account(rows, now=1000).faults

            self.assertEqual([seq for seq, _ in faults], [3])

    def test_a_start_that_this_node_asked_for_and_gave_up_on_is_matched_with_its_attempt(self):
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, "")]

        result = _check_account(rows, attempts=[check.StartAttempt(7, 990, 100)])

        self.assertEqual((result.faults, result.attempts_used), ([], [7]))

    def test_an_attempt_pays_for_one_movement(self):
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, ""),
                Row(4, 1001, -100, 0, ledger.CHILD_START, "")]

        result = _check_account(rows, attempts=[check.StartAttempt(7, 990, 100)])

        self.assertEqual([seq for seq, _ in result.faults], [4])

    def test_a_start_takes_no_more_than_its_attempt_sent(self):
        rows = [Row(3, 1000, -101, 0, ledger.CHILD_START, "")]

        self.assertEqual(len(_check_account(rows, attempts=[check.StartAttempt(7, 990, 100)]).faults), 1)

    def test_a_start_is_dated_within_the_window_of_its_attempt(self):
        attempt = check.StartAttempt(7, 1000, 100)
        window = check.start_grace_seconds() + check.CLOCK_SKEW_SECONDS

        def faults(ts):
            return _check_account([Row(3, ts, -100, 0, ledger.CHILD_START, "")], attempts=[attempt]).faults

        self.assertEqual(faults(1000 + window), [])
        self.assertEqual(len(faults(1000 + window + 1)), 1)
        self.assertEqual(faults(1000 - check.CLOCK_SKEW_SECONDS), [])
        self.assertEqual(len(faults(1000 - check.CLOCK_SKEW_SECONDS - 1)), 1)

    def test_a_start_with_a_child_of_ours_does_not_use_an_attempt(self):
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, ""),
                Row(4, 1000, 0, 0, ledger.CHILD_BOUND, "child", {"starts_seq": "3"})]

        result = _check_account(rows, attempts=[check.StartAttempt(7, 990, 100)])

        self.assertEqual((result.faults, result.attempts_used), ([], []))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TopUpAllowanceTests(unittest.TestCase):

    def test_a_top_up_of_a_child_of_ours_takes_no_more_than_was_sent(self):
        rows = [Row(3, 0, -900000, 0, ledger.TOP_UP, "child")]

        result = _check_account(rows, allowances={"child": 1000})

        self.assertEqual([seq for seq, _ in result.faults], [3])
        self.assertEqual(result.top_ups_taken, {"child": 1000})

    def test_the_top_ups_of_one_read_add_up_against_the_allowance(self):
        rows = [Row(3, 0, -600, 0, ledger.TOP_UP, "child"), Row(4, 0, -600, 0, ledger.TOP_UP, "child")]

        self.assertEqual([seq for seq, _ in _check_account(rows, allowances={"child": 1000}).faults], [4])

    def test_a_top_up_with_nothing_sent_is_a_fault(self):
        rows = [Row(3, 0, -1, 0, ledger.TOP_UP, "child")]

        self.assertEqual(len(_check_account(rows, allowances={"child": 0}).faults), 1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StaleMarkTests(unittest.TestCase):

    def test_failed_reads_are_priced_only_when_the_movements_show_their_range(self):
        pending = [(1, 4, 13, 100), (2, 4, 13, 100), (3, 20, 30, 100), (4, 4, 13, 100)]
        rows = [Row(4, 99, -1, 0, "debit", ""), Row(5, 100, -1, 0, "debit", ""),
                Row(6, 500, -1, 0, "debit", "")]

        priced = check.priced_failed_reads(pending[:2] + pending[2:3], rows, head_seq=6)

        # Reads 1 and 2: the movements reach the head, which is inside the range, and the
        # one dated after the read did not exist. Read 3 starts beyond the head.
        self.assertEqual(priced, {1: 2, 2: 2, 3: 0})
        # A range that the movements do not reach stays unpriced.
        self.assertEqual(check.priced_failed_reads([(4, 4, 13, 100)], rows[:2], head_seq=9), {})

    def test_a_mark_is_read_again_after_a_tenth_of_the_retention(self):
        tenth = ledger.min_peer_retention_days() * 86400 / 10

        self.assertTrue(check.stale_mark(None, 10 ** 9))
        self.assertFalse(check.stale_mark(10 ** 9 - tenth + 1, 10 ** 9))
        self.assertTrue(check.stale_mark(10 ** 9 - tenth, 10 ** 9))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PageRowsTests(unittest.TestCase):
    RATES = {"statement_query_mu": 100, "statement_row_mu": 10}

    def test_a_read_asks_for_what_it_can_pay_with_a_row_for_its_own_charge(self):
        # 100 for the call, 10 for each movement and one more for the charge.
        self.assertEqual(check.page_rows(100 + 5 * 10, self.RATES), 4)

    def test_a_balance_that_does_not_pay_for_the_head_asks_for_nothing(self):
        self.assertEqual(check.page_rows(100 + 10 - 1, self.RATES), 0)
        self.assertEqual(check.page_rows(99, self.RATES), 0)

    def test_a_node_that_charges_nothing_is_asked_for_a_full_page(self):
        self.assertEqual(check.page_rows(0, {}), ledger.max_statement_rows())

    def test_a_page_is_never_more_than_the_limit(self):
        self.assertEqual(check.page_rows(10 ** 12, self.RATES), ledger.max_statement_rows())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class QueryChargeTests(unittest.TestCase):
    RATES = {"statement_query_mu": 10, "statement_row_mu": 1}
    ROWS = [Row(2, 0, -14, 0, ledger.STATEMENT_QUERY, "")]

    def counters(self, **kwargs):
        return dict({"reads": 1, "rows_priced": 5, "verified": 1, "verified_mu": 14}, **kwargs)

    def test_what_was_asked_for_covers_what_was_charged(self):
        self.assertEqual(check.query_charge_problems(self.ROWS, self.counters(), self.RATES), [])

    def test_more_charges_than_reads_is_a_problem(self):
        problems = check.query_charge_problems(self.ROWS, self.counters(verified=2), self.RATES)

        self.assertEqual(len(problems), 1)

    def test_more_mu_than_the_price_of_what_was_asked_for_is_a_problem(self):
        problems = check.query_charge_problems(self.ROWS, self.counters(verified_mu=16), self.RATES)

        self.assertEqual(len(problems), 1)

    def test_nothing_to_check_without_a_charge_in_the_rows(self):
        self.assertEqual(check.query_charge_problems(
            [Row(2, 0, 5, 0, ledger.DEPOSIT, "")], self.counters(verified=99), self.RATES), [])


class _Database(unittest.TestCase):
    """An in-memory database with the real schema, for what is kept in it."""

    def setUp(self):
        import sqlite3

        from src.database.migrate import create_tables
        from src.database.sql_connection import SQLConnection

        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.execute("INSERT INTO peer (id) VALUES ('peer')")
        connection.commit()
        SQLConnection._connection = connection
        self.addCleanup(self._restore)

    def _restore(self):
        from src.database.sql_connection import SQLConnection
        SQLConnection._connection.close()
        SQLConnection._connection = self._original


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PenaltyTests(_Database):

    def setUp(self):
        super().setUp()
        patcher = patch("src.reputation_system.interface.update_peer_reputation")
        self.update = patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_fault_costs_one_penalty(self):
        for _ in range(3):
            check.penalise_once("peer", "acct", "statement_overcharged", "x", 5)

        self.update.assert_called_once_with(
            peer_id="peer", amount=check.STATEMENT_PENALTY, reason="statement_overcharged"
        )

    def test_the_same_fault_costs_nothing_after_a_restart(self):
        """What was scored is in the database, not in the memory of the process."""
        check.penalise_once("peer", "acct", "statement_overcharged", "x", 5)
        self.update.reset_mock()

        check.penalise_once("peer", "acct", "statement_overcharged", "x", 5)

        self.update.assert_not_called()

    def test_a_second_fault_of_the_same_reason_at_another_movement_is_scored(self):
        check.penalise_once("peer", "acct", "statement_overcharged", "x", 5)
        check.penalise_once("peer", "acct", "statement_overcharged", "y", 9)

        self.assertEqual(self.update.call_count, 2)

    def test_the_same_movement_for_another_reason_is_scored(self):
        check.penalise_once("peer", "acct", "statement_overcharged", "x", 5)
        check.penalise_once("peer", "acct", "statement_inconsistent", "x", 5)

        self.assertEqual(self.update.call_count, 2)

    def test_the_fault_is_kept_by_the_hash_of_the_account_and_not_the_account(self):
        check.penalise_once("peer", "a-client-id-that-is-a-secret", "statement_overcharged", "x", 5)

        rows = self.sc._execute("SELECT * FROM statement_faults").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account_ref"], ledger.account_ref("a-client-id-that-is-a-secret"))
        self.assertNotIn("secret", " ".join(str(v) for v in dict(rows[0]).values()))

    def test_the_faults_are_forgotten_with_the_retention(self):
        check.penalise_once("peer", "acct", "statement_overcharged", "x", 5)
        self.sc._execute("UPDATE statement_faults SET ts = 0")

        self.assertEqual(self.sc.prune_statement_faults(max_age_days=30), 1)

    def test_an_unreachable_peer_is_not_penalised(self):
        class Unavailable(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

        check.penalise_no_statement("peer", "acct", Unavailable())
        self.update.assert_not_called()

    @staticmethod
    def _error(code):
        class Coded(grpc.RpcError):
            def code(self):
                return code

        return Coded()

    def test_a_refusal_for_lack_of_balance_that_the_balance_still_pays_for_is_penalised(self):
        check.penalise_no_statement("peer", "acct", self._error(grpc.StatusCode.RESOURCE_EXHAUSTED),
                                    seq=3, pays=lambda: True)

        self.update.assert_called_once()

    def test_a_refusal_for_lack_of_balance_after_the_balance_fell_is_not_a_fault(self):
        check.penalise_no_statement("peer", "acct", self._error(grpc.StatusCode.RESOURCE_EXHAUSTED),
                                    seq=3, pays=lambda: False)

        self.update.assert_not_called()

    def test_a_child_that_the_peer_delegated_on_is_not_a_fault(self):
        error = self._error(grpc.StatusCode.UNIMPLEMENTED)

        check.penalise_no_statement("peer", "token", error, seq=3, child=True)
        self.update.assert_not_called()

        check.penalise_no_statement("peer", "acct", error, seq=3)
        self.update.assert_called_once()

    def test_a_peer_that_always_refuses_is_scored_once_a_day(self):
        with patch.object(check.time, "time", return_value=10 * 86400 + 5):
            for _ in range(3):
                check.penalise_no_statement("peer", "acct", Exception("refused"), seq=3)
        self.assertEqual(self.update.call_count, 1)

        with patch.object(check.time, "time", return_value=11 * 86400 + 5):
            check.penalise_no_statement("peer", "acct", Exception("refused"), seq=3)
        self.assertEqual(self.update.call_count, 2)

    def test_no_statement_is_one_fault_for_the_movement_that_was_missing(self):
        check.penalise_no_statement("peer", "acct", Exception("refused"), seq=3)
        check.penalise_no_statement("peer", "acct", Exception("refused"), seq=3)
        check.penalise_no_statement("peer", "acct", Exception("refused"), seq=4)

        self.assertEqual(self.update.call_count, 2)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CountedReadTests(_Database):
    """Each read is counted when it is asked, before the peer can charge it."""

    def read(self, answer=None, error=None, **kwargs):
        statement = celaut_pb2.BalanceStatement()
        for _ in range(3):
            statement.movements.add()
        with patch("src.manager.balance_statement.fetch",
                   side_effect=error, return_value=answer if answer is not None else statement) as fetch:
            try:
                check.read("peer", "client", from_seq=4, **kwargs)
            except Exception:
                pass
        return fetch

    def test_a_read_that_is_answered_counts_the_rows_of_its_answer(self):
        self.read(rows=10)

        self.assertEqual(self.sc.get_statement_counters("peer")["reads"], 1)
        # The row of the charge, and the three movements.
        self.assertEqual(self.sc.get_statement_counters("peer")["rows_priced"], 4)

    def test_a_read_that_fails_counts_all_that_it_asked_for(self):
        self.read(error=RuntimeError("timeout"), rows=10)

        counters = self.sc.get_statement_counters("peer")
        self.assertEqual((counters["reads"], counters["rows_priced"]), (1, 11))

    def test_a_read_that_fails_is_written_down_with_its_range_and_counts_what_it_asked_for(self):
        self.read(error=RuntimeError("timeout"), rows=10)

        pending = self.sc.failed_statement_reads("peer", ledger.account_ref("client"))
        self.assertEqual([(a, b) for _, a, b, _ in pending], [(4, 13)])

    def test_a_failed_read_is_priced_with_the_rows_that_the_account_had_then(self):
        for _ in range(9):
            self.read(error=RuntimeError("timeout"), rows=1000)
        pending = self.sc.failed_statement_reads("peer", ledger.account_ref("client"))
        self.assertEqual(self.sc.get_statement_counters("peer")["rows_priced"], 9 * 1001)

        # The account holds 6 movements, all older than the reads.
        rows = [Row(seq, 0, -1, 0, "debit", "") for seq in range(4, 10)]
        check.settle_failed_reads("peer", "client", rows, head_seq=9)

        # The row of the charge of each read, and the 6 movements that existed for each.
        self.assertEqual(self.sc.get_statement_counters("peer")["rows_priced"], 9 * 1 + 9 * 6)
        self.assertEqual(self.sc.failed_statement_reads("peer", ledger.account_ref("client")), [])
        self.assertEqual(len(pending), 9)

    def test_reads_that_time_out_do_not_hide_charges_that_were_never_due(self):
        """Nine reads of 1000 rows that time out, and one answered with none: the peer
        cannot charge ten statements of 1001 MU for an account that has no movement."""
        rates = {"statement_query_mu": 1, "statement_row_mu": 1}
        for _ in range(9):
            self.read(error=RuntimeError("timeout"), rows=1000)
        self.read(answer=celaut_pb2.BalanceStatement(), rows=1000)
        check.settle_failed_reads("peer", "client", [], head_seq=3)
        self.sc.add_statement_queries_verified("peer", 10, 10 * 1001)

        problems = check.query_charge_problems(
            [Row(5, 0, -1001, 0, "statement_query", "")] * 10,
            self.sc.get_statement_counters("peer"), rates)

        self.assertTrue(problems)

    def test_a_page_is_asked_as_a_range(self):
        fetch = self.read(rows=10)

        self.assertEqual((fetch.call_args.kwargs["from_seq"], fetch.call_args.kwargs["to_seq"]),
                         (4, 13))

    def test_the_node_decides_when_no_size_is_asked(self):
        fetch = self.read()

        self.assertEqual(fetch.call_args.kwargs["to_seq"], 0)


if __name__ == "__main__":
    unittest.main()

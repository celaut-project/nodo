"""Checking a peer's statements (#570): that they add up, and that each charge follows
its rule. A peer that fails either loses reputation; nothing else may."""

import unittest
from unittest.mock import MagicMock, patch

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

    def test_a_movement_dated_back_is_not_a_fault_and_takes_the_date_before_it(self):
        """New problem 8 (review 3): the clock of an honest peer can step back."""
        book = chained((100, "start"), (-30, "build"), (-5, "maintenance"))
        book.rows[2]["ts"] = book.rows[1]["ts"] - 600
        book.rows[2]["hash"] = ledger.movement_hash(
            book.rows[2]["prev_hash"], 3, book.rows[2]["ts"], -5, book.rows[2]["balance_after"],
            "maintenance", None, None)

        chain, _ = verify(book, Mark.new_account("acct", ts=0))

        self.assertIsNone(chain.problem)
        self.assertEqual(chain.rows[2].ts, book.rows[1]["ts"])

    def test_a_movement_dated_before_the_mark_is_not_a_fault_and_takes_the_date_of_the_mark(self):
        book = chained((100, "start"), (-30, "build"), (-5, "maintenance"))
        mark = book.mark_at(2)
        later = Mark(mark.seq, mark.hash, mark.balance, mark.ts + 600)

        chain, statement = verify(book, later)

        self.assertIsNone(chain.problem)
        self.assertEqual(chain.rows[0].ts, later.ts)
        # The mark does not go back either.
        self.assertGreaterEqual(check.mark_after(chain, statement, later).ts, later.ts)

    def test_a_clock_that_steps_back_between_two_reads_scores_nothing(self):
        """Review 3, question 5: a step back of 600 s between two reads."""
        book = chained((100, "start"), (-30, "build"))
        first, statement = verify(book, Mark.new_account("acct", ts=0))
        mark = check.mark_after(first, statement, Mark.new_account("acct", ts=0))
        book.add(-5, "maintenance", ts=book.ts - 600)

        chain, _ = verify(book, mark, ts=book.ts + 1, now=book.ts + 1)

        self.assertIsNone(chain.problem)

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

    def test_a_statement_dated_far_from_our_clock_is_a_fault_that_shows_no_movement(self):
        book = chained((100, "start"))

        chain, statement = verify(book, ts=10 ** 9, now=1000)

        self.assertIsNone(chain.rows)
        self.assertIn("is dated", chain.problem)
        self.assertTrue(chain.clock)
        self.assertEqual(chain.fault_seq, check.CLOCK_FAULT_SEQ)

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


def self_bound(seq, child="child", starts_seq=None):
    return Row(seq, 0, 0, 0, ledger.CHILD_BOUND, child,
               {"starts_seq": str(seq - 1 if starts_seq is None else starts_seq)})


def _check_account(rows, starts=None, attempts=(), reservations=None, now=10 ** 6, **kwargs):
    return check.account_faults(
        rows, {"child": 100} if starts is None else starts, attempts,
        [check.TopUpReservation(1, "child", 0, 1000)] if reservations is None else reservations,
        now=now, **kwargs)


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
        self.assertEqual(result.top_ups_taken, {1: 50})

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


def attempt(id, ts=990, sent=100, ref="", **kwargs):
    return check.StartAttempt(id, ts, sent, ref, **kwargs)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StartAttemptTests(unittest.TestCase):
    """A ``child_start`` is judged against what this node asked for, and nothing else:
    nothing waits, and nothing is excused for its age."""

    def test_a_start_that_nobody_asked_for_is_a_fault(self):
        for child_row in ([], [Row(4, 0, 0, 0, ledger.CHILD_BOUND, "stranger", {"starts_seq": "3"})]):
            rows = [Row(3, 1000, -900000, 0, ledger.CHILD_START, "")] + child_row

            faults = _check_account(rows, now=1000).faults

            self.assertEqual([seq for seq, _ in faults], [3])

    def test_a_start_that_this_node_asked_for_holds_its_attempt_until_its_child_is_named(self):
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, "")]

        result = _check_account(rows, attempts=[attempt(7)])

        self.assertEqual((result.faults, result.attempts), ([], {7: (3, 100, 1000, False)}))

    def test_a_start_whose_child_this_node_never_learnt_closes_an_attempt_with_no_answer(self):
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, ""),
                Row(4, 1000, 0, 0, ledger.CHILD_BOUND, "stranger", {"starts_seq": "3"})]

        result = _check_account(rows, attempts=[attempt(7), attempt(8, ref="child")])

        self.assertEqual((result.faults, result.attempts), ([], {7: (3, 100, 1000, True)}))

    def test_an_attempt_holds_one_movement(self):
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, ""),
                Row(4, 1001, -100, 0, ledger.CHILD_START, "")]

        result = _check_account(rows, attempts=[attempt(7)])

        self.assertEqual([seq for seq, _ in result.faults], [4])

    def test_a_start_takes_no_more_than_its_attempt_sent(self):
        rows = [Row(3, 1000, -101, 0, ledger.CHILD_START, "")]

        self.assertEqual(len(_check_account(rows, attempts=[attempt(7)]).faults), 1)

    def test_a_start_is_dated_within_the_window_of_its_attempt(self):
        window = check.start_grace_seconds() + check.CLOCK_SKEW_SECONDS

        def faults(ts):
            return _check_account([Row(3, ts, -100, 0, ledger.CHILD_START, "")],
                                  attempts=[attempt(7, ts=1000)]).faults

        self.assertEqual(faults(1000 + window), [])
        self.assertEqual(len(faults(1000 + window + 1)), 1)
        self.assertEqual(faults(1000 - check.CLOCK_SKEW_SECONDS), [])
        self.assertEqual(len(faults(1000 - check.CLOCK_SKEW_SECONDS - 1)), 1)

    def test_the_start_of_a_child_of_ours_is_paid_once(self):
        rows = [Row(3, 0, -100, 0, ledger.CHILD_START, ""), self_bound(4),
                Row(5, 0, -100, 0, ledger.CHILD_START, ""), self_bound(6, starts_seq=5)]

        result = _check_account(rows)

        self.assertEqual([seq for seq, _ in result.faults], [5])

    def test_a_start_that_an_earlier_read_showed_is_not_paid_again(self):
        rows = [Row(3, 0, -100, 0, ledger.CHILD_START, ""), self_bound(4)]

        result = check.account_faults(rows, {"child": 100}, [], [], now=10 ** 6, started=["child"])

        self.assertEqual([seq for seq, _ in result.faults], [3])

    def test_a_start_of_a_child_of_ours_closes_the_attempt_of_that_child(self):
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, ""),
                Row(4, 1000, 0, 0, ledger.CHILD_BOUND, "child", {"starts_seq": "3"})]

        result = _check_account(rows, attempts=[attempt(7), attempt(8, ref="child")])

        self.assertEqual((result.faults, result.attempts), ([], {8: (3, 100, 1000, True)}))

    def test_a_refund_sets_the_attempt_free_for_the_next_candidate(self):
        """New problem 1: one start, two candidates, a read during each."""
        held = attempt(7, ref="child", start_seq=3, start_mu=100, start_ts=1000)
        rows = [Row(4, 1001, 100, 0, ledger.REFUND, "", {"refunds_seq": "3"}),
                Row(5, 1002, -100, 0, ledger.CHILD_START, "")]

        result = _check_account(rows, attempts=[held])

        self.assertEqual((result.faults, result.attempts), ([], {7: (5, 100, 1002, False)}))

    def test_two_starts_at_once_change_attempts_when_their_children_are_named(self):
        """New problem 5: the movement of B held A; the movement of A goes to A, and the
        one of B to B."""
        a = attempt(7, sent=1000, ref="child-a", start_seq=3, start_mu=1000, start_ts=1000)
        b = attempt(8, sent=1000, ref="child-b")
        rows = [Row(4, 1001, -1000, 0, ledger.CHILD_START, ""),
                Row(5, 1002, 0, 0, ledger.CHILD_BOUND, "child-b", {"starts_seq": "3"}),
                Row(6, 1003, 0, 0, ledger.CHILD_BOUND, "child-a", {"starts_seq": "4"})]

        result = _check_account(rows, starts={"child-a": 1000, "child-b": 1000}, attempts=[a, b])

        self.assertEqual(result.faults, [])
        self.assertEqual(result.attempts, {7: (4, 1000, 1001, True), 8: (3, 1000, 1000, True)})

    def test_an_attempt_holds_the_movement_that_fits_it_best(self):
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, "")]

        result = _check_account(rows, attempts=[attempt(7, sent=1000), attempt(8, sent=100)])

        self.assertEqual(list(result.attempts), [8])

    def test_a_refused_attempt_holds_no_movement(self):
        """New problem 6."""
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, "")]

        faults = _check_account(rows, attempts=[attempt(7, refused_at=995)]).faults

        self.assertEqual([seq for seq, _ in faults], [3])

    def test_a_movement_of_a_refused_attempt_must_be_given_back_by_the_head(self):
        held = attempt(7, start_seq=3, start_mu=100, start_ts=1000, refused_at=1005)
        rows = [Row(4, 1010, 5, 0, ledger.CREDIT, "")]

        self.assertEqual(_check_account(rows, attempts=[held], now=1006).faults, [])
        result = _check_account(rows, attempts=[held], now=1006, reaches_head=True)
        self.assertEqual([seq for seq, _ in result.faults], [3])
        given_back = rows + [Row(5, 1011, 100, 0, ledger.REFUND, "", {"refunds_seq": "3"})]
        self.assertEqual(
            _check_account(given_back, attempts=[held], now=1006, reaches_head=True).faults, [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TopUpReservationTests(unittest.TestCase):

    def test_a_top_up_of_a_child_of_ours_takes_no_more_than_was_sent(self):
        rows = [Row(3, 0, -900000, 0, ledger.TOP_UP, "child")]

        result = _check_account(rows)

        self.assertEqual([seq for seq, _ in result.faults], [3])
        self.assertEqual(result.top_ups_taken, {1: 1000})

    def test_the_top_ups_of_one_read_add_up_against_the_reservation(self):
        rows = [Row(3, 0, -600, 0, ledger.TOP_UP, "child"), Row(4, 0, -600, 0, ledger.TOP_UP, "child")]

        self.assertEqual([seq for seq, _ in _check_account(rows).faults], [4])

    def test_a_top_up_with_nothing_sent_is_a_fault(self):
        rows = [Row(3, 0, -1, 0, ledger.TOP_UP, "child")]

        self.assertEqual(len(_check_account(rows, reservations=[]).faults), 1)

    def test_a_top_up_is_dated_near_its_reservation(self):
        """New problem 7: a reservation does not wait for ever."""
        window = check.top_up_grace_seconds() + check.CLOCK_SKEW_SECONDS
        reservation = [check.TopUpReservation(1, "child", 1000, 300)]

        def faults(ts):
            return _check_account([Row(3, ts, -300, 0, ledger.TOP_UP, "child")],
                                  reservations=reservation).faults

        self.assertEqual(faults(1000 + window), [])
        self.assertEqual(len(faults(1000 + window + 1)), 1)
        self.assertEqual(len(faults(1000 + 20 * 86400)), 1)
        self.assertEqual(len(faults(1000 - check.CLOCK_SKEW_SECONDS - 1)), 1)

    def test_two_top_ups_take_from_their_own_reservations(self):
        reservations = [check.TopUpReservation(1, "child", 1000, 300),
                        check.TopUpReservation(2, "child", 5000, 200)]
        rows = [Row(3, 1001, -300, 0, ledger.TOP_UP, "child"),
                Row(4, 5001, -200, 0, ledger.TOP_UP, "child")]

        result = _check_account(rows, reservations=reservations)

        self.assertEqual((result.faults, result.top_ups_taken), ([], {1: 300, 2: 200}))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StaleMarkTests(unittest.TestCase):

    def test_failed_reads_of_a_child_are_priced_when_the_movements_show_their_range(self):
        pending = [(1, 4, 13, 100, None, 0), (2, 4, 13, 100, None, 0), (3, 20, 30, 100, None, 0)]
        rows = [Row(4, 99, -1, 0, "debit", ""), Row(5, 100, -1, 0, "debit", ""),
                Row(6, 500, -1, 0, "debit", "")]

        priced, progress = check.priced_child_reads(pending, rows, head_seq=6)

        # Reads 1 and 2: the movements reach the head, which is inside the range, and the
        # one dated after the read did not exist. Read 3 starts beyond the head.
        self.assertEqual((priced, progress), ({1: 2, 2: 2, 3: 0}, {}))

    def test_a_range_that_a_page_shows_in_part_is_priced_by_the_next_pages(self):
        """New problem 4: a range of 10 read in pages of 4."""
        rows = [Row(seq, 99 if seq < 8 else 500, -1, 0, "debit", "") for seq in range(4, 14)]

        priced, progress = check.priced_child_reads([(1, 4, 13, 100, None, 0)], rows[:4], 13)
        self.assertEqual((priced, progress), ({}, {1: (7, 4)}))
        priced, progress = check.priced_child_reads([(1, 4, 13, 100, 7, 4)], rows[4:8], 13)
        self.assertEqual((priced, progress), ({}, {1: (11, 4)}))
        priced, progress = check.priced_child_reads([(1, 4, 13, 100, 11, 4)], rows[8:], 13)
        self.assertEqual((priced, progress), ({1: 4}, {}))
        # A page that does not go on from what was seen changes nothing.
        self.assertEqual(check.priced_child_reads([(1, 4, 13, 100, 7, 4)], rows[5:], 13), ({}, {}))

    def test_a_failed_read_of_our_account_is_priced_by_the_seq_of_its_charge(self):
        """New problem 4: the rows before its charge existed, and no row after it did,
        whatever their dates."""
        def charge(seq, from_seq):
            return Row(seq, 0, -101, 0, ledger.STATEMENT_QUERY, "",
                       {"from_seq": str(from_seq), "rows": "300", "to_seq": "1003"})

        rows = [Row(4, 0, 1, 0, "credit", ""), Row(5, 0, 1, 0, "credit", ""), charge(6, 4)] + \
               [Row(seq, 0, 0, 0, "credit", "") for seq in range(7, 300)]

        priced = check.priced_own_reads([(1, 4, 1003, 100, None, 0)], rows, now=200,
                                        reaches_head=True)

        self.assertEqual(priced, {1: 2})

    def test_a_failed_read_of_our_account_with_no_charge_counts_for_nothing(self):
        rows = [Row(4, 0, 1, 0, "credit", "")]
        late = 100 + check.peer_call_timeout() + check.CLOCK_SKEW_SECONDS + 1

        self.assertEqual(check.priced_own_reads([(1, 4, 13, 100, None, 0)], rows, now=late,
                                                reaches_head=False), {})
        self.assertEqual(check.priced_own_reads([(1, 4, 13, 100, None, 0)], rows, now=150,
                                                reaches_head=True), {})
        self.assertEqual(check.priced_own_reads([(1, 4, 13, 100, None, 0)], rows, now=late,
                                                reaches_head=True), {1: 0})

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
class SignedFloorTests(unittest.TestCase):
    """Points 9 and 10: the least that our account holds, from what the peer signed."""
    RATES = {"statement_query_mu": 10, "statement_row_mu": 1}

    def test_the_floor_is_the_signed_balance_less_what_this_node_caused(self):
        counters = {"reads": 3, "rows_priced": 20, "verified": 2, "verified_mu": 25}

        # Three reads at 10 and 20 rows at 1 cost 50 at most, and 25 showed.
        self.assertEqual(check.signed_floor(1000, 100, 50, counters, self.RATES),
                         1000 - 100 - 50 - 25)

    def test_the_charges_still_to_come_are_one_page_at_most_for_each_read(self):
        counters = {"reads": 10, "rows_priced": 10 ** 6, "verified": 9, "verified_mu": 0}
        page = 10 + 1 * (ledger.max_statement_rows() + 1)

        self.assertEqual(check.signed_floor(10 ** 6, 0, 0, counters, self.RATES), 10 ** 6 - page)

    def test_only_what_can_still_be_taken_after_the_mark_lowers_the_floor(self):
        now = 10 ** 6
        books = MagicMock()
        books.get_peer_statement_mark.return_value = {"seq": 5, "hash": "h", "balance_mu": 5000,
                                                      "ts": now}
        old = now - check.start_grace_seconds() - 3 * check.CLOCK_SKEW_SECONDS
        books.open_start_attempts.return_value = [
            (1, now, 100, "", None, 0, 0, None),     # free: it may still take 100
            (2, old, 700, "", None, 0, 0, None),     # its window ended before the mark
            (3, now, 200, "", 4, 200, now, None),    # its movement is before the mark
            (4, now, 300, "", None, 0, 0, now)]      # refused
        books.top_up_reservations.return_value = [(1, "child", now, 50), (2, "child", 0, 900)]
        books.get_statement_counters.return_value = {"reads": 0, "rows_priced": 0,
                                                     "verified": 0, "verified_mu": 0}
        with patch.object(check, "stored_rates", return_value=self.RATES):
            self.assertEqual(check.floor_on("peer", books), 5000 - 100 - 50)

    def test_a_floor_that_cannot_be_read_is_zero(self):
        books = MagicMock()
        books.get_peer_statement_mark.side_effect = RuntimeError("locked")

        self.assertEqual(check.floor_on("peer", books), 0)


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

    def test_a_transport_failure_of_a_peer_that_answers_is_a_refusal_once_the_mark_is_old(self):
        """New problem 3: the peer chooses the code that it answers with."""
        error = self._error(grpc.StatusCode.UNAVAILABLE)
        now = check.time.time()
        old = now - check.refusal_age_seconds() - 1

        check.penalise_no_statement("peer", "acct", error, seq=3, answered=True, mark_ts=now)
        check.penalise_no_statement("peer", "acct", error, seq=3, answered=False, mark_ts=old)
        check.penalise_no_statement("peer", "acct", error, seq=3, answered=True, mark_ts=None)
        self.update.assert_not_called()

        check.penalise_no_statement("peer", "acct", error, seq=3, answered=True, mark_ts=old)
        self.update.assert_called_once()

    def test_a_fault_with_a_window_is_scored_again_only_after_the_window(self):
        with patch.object(check.time, "time", return_value=10 * 86400 - 60):
            check.penalise_once("peer", "acct", "statement_inconsistent", "x", -1, window=86400)
        with patch.object(check.time, "time", return_value=10 * 86400 + 60):
            check.penalise_once("peer", "acct", "statement_inconsistent", "x", -1, window=86400)
        self.assertEqual(self.update.call_count, 1)

        with patch.object(check.time, "time", return_value=11 * 86400 - 60):
            check.penalise_once("peer", "acct", "statement_inconsistent", "x", -1, window=86400)
        self.assertEqual(self.update.call_count, 2)

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

    def test_a_read_of_our_account_that_fails_counts_for_its_charge_only(self):
        """Until its charge shows, it is priced at 0 rows (new problem 4)."""
        self.read(error=RuntimeError("timeout"), rows=10)

        counters = self.sc.get_statement_counters("peer")
        self.assertEqual((counters["reads"], counters["rows_priced"]), (1, 1))

    def test_a_read_of_a_child_that_fails_counts_all_that_it_asked_for_once(self):
        """A second read of the same range adds nothing (new problem 3)."""
        for _ in range(3):
            with patch("src.manager.balance_statement.fetch", side_effect=RuntimeError("timeout")):
                with self.assertRaises(RuntimeError):
                    check.read("peer", "client", token="child", from_seq=4, rows=10)

        counters = self.sc.get_statement_counters("peer")
        self.assertEqual((counters["reads"], counters["rows_priced"]), (3, 3 + 10))

    def test_a_read_that_fails_is_written_down_with_its_range_and_counts_what_it_asked_for(self):
        self.read(error=RuntimeError("timeout"), rows=10)

        pending = self.sc.failed_statement_reads("peer", ledger.account_ref("client"))
        self.assertEqual([(a, b) for _, a, b, *_ in pending], [(4, 13)])

    def test_a_failed_read_is_priced_with_the_rows_before_its_charge(self):
        for _ in range(9):
            self.read(error=RuntimeError("timeout"), rows=1000)
        pending = self.sc.failed_statement_reads("peer", ledger.account_ref("client"))
        self.assertEqual(self.sc.get_statement_counters("peer")["rows_priced"], 9)

        # The account holds 6 movements, and then the charge of each of the 9 reads.
        rows = [Row(seq, 0, -1, 0, "debit", "") for seq in range(4, 10)] + [
            Row(seq, 0, -1007, 0, ledger.STATEMENT_QUERY, "",
                {"from_seq": "4", "rows": "6", "to_seq": "1003"}) for seq in range(10, 19)]
        check.settle_failed_reads("peer", "client", rows, head_seq=18, own=True)

        # The row of the charge of each read, and the movements before each charge.
        self.assertEqual(self.sc.get_statement_counters("peer")["rows_priced"],
                         9 + sum(range(6, 15)))
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

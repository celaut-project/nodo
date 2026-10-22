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


def tick(amount, seq, seconds=10, mem=GIB, ts=None):
    import json
    detail = json.dumps({"seconds": seconds, "mem_limit": mem, "disk_space": 0,
                         "cpu_period": 100000, "cpu_quota": 100000})
    return Row(seq=seq, ts=ts if ts is not None else 100 + seq * 10, amount=amount,
               balance_after=0, kind=ledger.MAINTENANCE, ref="",
               detail=check._detail(detail))


RATES = {"ram_mu_per_gib_second": 100, "cpu_mu_per_vcpu_second": 1000,
         "scarcity_max_multiplier": 2}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChildRuleTests(unittest.TestCase):

    def start_rows(self, got=100, build=-30, commission=0):
        rows = [Row(1, 100, got, got, ledger.START, "")]
        if commission:
            rows.append(Row(2, 100, commission, got + commission, ledger.COMMISSION, ""))
        rows.append(Row(len(rows) + 1, 100, build, 0, ledger.BUILD, ""))
        return rows

    def test_a_start_within_its_quote_is_fine(self):
        self.assertEqual(check.child_problems(
            self.start_rows(), mark=Mark(0, "", 0), rates=RATES, sent_peer_mu=100,
            start_bound_peer_mu=30,
        ), [])

    def test_a_start_that_got_less_than_was_sent_is_an_overcharge(self):
        problems = check.child_problems(self.start_rows(got=90), mark=Mark(0, "", 0), rates=RATES,
                                        sent_peer_mu=100, start_bound_peer_mu=30)
        self.assertEqual(len(problems), 1)

    def test_a_build_and_commission_above_the_quote_are_an_overcharge(self):
        problems = check.child_problems(
            self.start_rows(build=-25, commission=-10), mark=Mark(0, "", 0), rates=RATES,
            sent_peer_mu=100, start_bound_peer_mu=30,
        )
        self.assertEqual(len(problems), 1)

    def test_a_statement_with_no_start_is_an_overcharge(self):
        self.assertTrue(check.child_problems(
            [], mark=Mark(0, "", 0), rates=RATES, sent_peer_mu=100, start_bound_peer_mu=30,
        ))

    def test_a_build_at_a_tick_is_an_overcharge(self):
        rows = [Row(4, 140, -30, 0, ledger.BUILD, "")]
        self.assertTrue(check.child_problems(rows, mark=Mark(3, "", 30), rates=RATES))

    def test_a_tick_is_bounded_by_its_resources_at_the_rates_of_the_peer(self):
        # 1 GiB and 1 vCPU for 10 s: (101 + 1001) * 10 * 2 = 22040.
        self.assertEqual(check.maintenance_bound(tick(0, 1).detail, RATES), 22040)
        self.assertEqual(check.child_problems([tick(-22040, 4)], mark=Mark(3, "", 0), rates=RATES), [])
        self.assertTrue(check.child_problems([tick(-22041, 4)], mark=Mark(3, "", 0), rates=RATES))

    def test_a_tick_that_names_no_resources_is_not_bounded(self):
        row = Row(4, 140, -10 ** 9, 0, ledger.MAINTENANCE, "", {"peer_mu": "1"})
        self.assertEqual(check.child_problems([row], mark=Mark(3, "", 0), rates=RATES), [])

    def test_ticks_cannot_charge_more_time_than_passed(self):
        rows = [tick(-1, seq, ts=200) for seq in (4, 5, 6)]

        self.assertEqual(check.child_problems(rows[:2], mark=Mark(3, "", 0, ts=190), rates=RATES), [])
        self.assertTrue(check.child_problems(rows, mark=Mark(3, "", 0, ts=190), rates=RATES))

    def test_what_the_child_does_with_its_own_balance_is_not_a_charge_of_the_peer(self):
        rows = [Row(4, 140, -50, 0, kind, "") for kind in (
            ledger.CHILD_START, ledger.TOP_UP, ledger.TUNNEL, ledger.RESOURCE_CHANGE,
            ledger.STATEMENT_QUERY,
        )]
        self.assertEqual(check.child_problems(rows, mark=Mark(3, "", 0), rates=RATES), [])

    def test_an_unknown_charge_is_an_overcharge(self):
        rows = [Row(4, 140, -1, 0, "fee", ""), Row(5, 140, -1, 0, ledger.DEBIT, "")]
        self.assertEqual(len(check.child_problems(rows, mark=Mark(3, "", 0), rates=RATES)), 2)

    def test_consumption_leaves_out_what_the_father_moved(self):
        rows = [Row(4, 0, -10, 0, ledger.MAINTENANCE, ""),
                Row(5, 0, 500, 0, ledger.TOP_UP, ""),
                Row(6, 0, -200, 0, ledger.WITHDRAWAL, ""),
                Row(7, 0, -20, 0, ledger.CHILD_START, "grandchild")]
        self.assertEqual(check.consumption(rows), 30)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AccountRuleTests(unittest.TestCase):
    STARTS = {"child": 100}

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
        self.assertEqual(check.account_problems(rows, self.STARTS, now_ts=10 ** 6), ([], None))

    def test_a_start_above_what_was_sent_is_an_overcharge(self):
        rows = [Row(3, 0, -101, 0, ledger.CHILD_START, ""), self.bound(4)]
        self.assertEqual(len(check.account_problems(rows, self.STARTS, now_ts=0)[0]), 1)

    def test_a_start_bound_to_a_child_that_is_not_ours_is_an_overcharge_once_it_is_old(self):
        rows = [Row(3, 0, -100, 0, ledger.CHILD_START, ""), self.bound(4, child="stranger")]
        problems, stop_at = check.account_problems(rows, self.STARTS, now_ts=10 ** 6)

        self.assertEqual((len(problems), stop_at), (1, None))

    def test_a_start_in_progress_stops_the_check_until_it_is_known(self):
        rows = [Row(3, 1000, -100, 0, ledger.CHILD_START, ""),
                Row(4, 1000, -999, 0, ledger.DEBIT, "")]

        self.assertEqual(check.account_problems(rows, self.STARTS, now_ts=1001), ([], 3))

    def test_a_start_that_nobody_asked_for_is_an_overcharge_once_it_is_old(self):
        rows = [Row(3, 0, -100, 0, ledger.CHILD_START, "")]
        problems, stop_at = check.account_problems(rows, self.STARTS, now_ts=10 ** 6)

        self.assertEqual((len(problems), stop_at), (1, None))

    def test_a_start_that_was_given_back_is_fine(self):
        rows = [Row(3, 0, -100, 0, ledger.CHILD_START, ""),
                Row(4, 0, 100, 0, ledger.REFUND, "", {"refunds_seq": "3"})]
        self.assertEqual(check.account_problems(rows, self.STARTS, now_ts=10 ** 6), ([], None))

    def test_a_charge_this_node_did_not_cause_is_an_overcharge(self):
        rows = [Row(3, 0, -5, 0, ledger.DEBIT, ""),
                Row(4, 0, -5, 0, ledger.TOP_UP, "not-ours"),
                Row(5, 0, -5, 0, ledger.MAINTENANCE, "")]
        self.assertEqual(len(check.account_problems(rows, self.STARTS, now_ts=0)[0]), 3)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PenaltyTests(unittest.TestCase):

    def setUp(self):
        check._penalised.clear()
        patcher = patch("src.reputation_system.interface.update_peer_reputation")
        self.update = patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_fault_costs_one_penalty(self):
        for _ in range(3):
            check.penalise_once("peer", "acct", "statement_overcharged", "x")

        self.update.assert_called_once_with(
            peer_id="peer", amount=check.STATEMENT_PENALTY, reason="statement_overcharged"
        )

    def test_an_unreachable_peer_is_not_penalised(self):
        class Unavailable(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

        check.penalise_no_statement("peer", "acct", Unavailable())
        self.update.assert_not_called()

    def test_a_refusal_while_our_account_cannot_pay_is_not_penalised(self):
        with patch.object(check, "_short_of_statement_price", return_value=True):
            check.penalise_no_statement("peer", "acct", Exception("No balance statement."))
        self.update.assert_not_called()

    def test_a_refusal_while_our_account_can_pay_is_penalised(self):
        with patch.object(check, "_short_of_statement_price", return_value=False):
            check.penalise_no_statement("peer", "acct", Exception("No balance statement."))
        self.update.assert_called_once()


if __name__ == "__main__":
    unittest.main()

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
    from src.utils.monetary import GIB
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def statement(*movements, balance=None, truncated=False):
    """A statement from (seq, amount, kind[, token, detail, ts]) tuples, adding up from 0
    before the first one unless it is told otherwise with ``balance``."""
    out = celaut_pb2.BalanceStatement(truncated=truncated)
    running = 0
    for movement in movements:
        seq, amount, kind = movement[:3]
        token = movement[3] if len(movement) > 3 else ""
        detail = movement[4] if len(movement) > 4 else ""
        ts = movement[5] if len(movement) > 5 else 100 + seq
        running += amount
        out.movements.add(seq=seq, ts=ts, kind=kind, instance_ref=token, detail=detail,
                          amount=celaut_pb2.Amount(n=str(amount)),
                          balance_after=celaut_pb2.Amount(n=str(running)))
    out.balance.n = str(running if balance is None else balance)
    return out


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AddsUpTests(unittest.TestCase):

    def test_a_new_account_adds_up_from_zero(self):
        rows, problem = check.new_rows(statement((1, 100, "start"), (2, -30, "build")), Mark(0, 0))

        self.assertIsNone(problem)
        self.assertEqual([r.seq for r in rows], [1, 2])

    def test_a_gap_does_not_add_up(self):
        s = statement((1, 100, "start"), (3, -30, "build"))

        self.assertEqual(check.new_rows(s, Mark(0, 0))[0], None)
        self.assertIn("missing", check.new_rows(s, Mark(0, 0))[1])

    def test_a_balance_that_is_not_the_last_movement_does_not_add_up(self):
        s = statement((1, 100, "start"), balance=99)

        self.assertIsNotNone(check.new_rows(s, Mark(0, 0))[1])

    def test_only_the_movements_after_the_mark_are_new(self):
        s = statement((1, 100, "start"), (2, -30, "build"), (3, -5, "maintenance"))

        rows, problem = check.new_rows(s, Mark(2, 70))

        self.assertIsNone(problem)
        self.assertEqual([r.seq for r in rows], [3])

    def test_a_mark_that_changed_does_not_add_up(self):
        s = statement((1, 100, "start"), (2, -30, "build"))

        self.assertIn("is not the movement", check.new_rows(s, Mark(2, 71))[1])

    def test_a_balance_that_moved_with_no_movement_does_not_add_up(self):
        empty = celaut_pb2.BalanceStatement(balance=celaut_pb2.Amount(n="69"))

        self.assertIsNotNone(check.new_rows(empty, Mark(2, 70))[1])

    def test_an_answer_cut_by_the_node_cannot_be_checked_and_is_no_fault(self):
        s = statement((1, 0, "start"), (5, 70, "maintenance"), truncated=True, balance=70)
        s.movements.pop(0)

        self.assertEqual(check.new_rows(s, Mark(2, 70)), (None, None))


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
            self.start_rows(), mark=Mark(0, 0), rates=RATES, sent_peer_mu=100,
            start_bound_peer_mu=30,
        ), [])

    def test_a_start_that_got_less_than_was_sent_is_an_overcharge(self):
        problems = check.child_problems(self.start_rows(got=90), mark=Mark(0, 0), rates=RATES,
                                        sent_peer_mu=100, start_bound_peer_mu=30)
        self.assertEqual(len(problems), 1)

    def test_a_build_and_commission_above_the_quote_are_an_overcharge(self):
        problems = check.child_problems(
            self.start_rows(build=-25, commission=-10), mark=Mark(0, 0), rates=RATES,
            sent_peer_mu=100, start_bound_peer_mu=30,
        )
        self.assertEqual(len(problems), 1)

    def test_a_statement_with_no_start_is_an_overcharge(self):
        self.assertTrue(check.child_problems(
            [], mark=Mark(0, 0), rates=RATES, sent_peer_mu=100, start_bound_peer_mu=30,
        ))

    def test_a_build_at_a_tick_is_an_overcharge(self):
        rows = [Row(4, 140, -30, 0, ledger.BUILD, "")]
        self.assertTrue(check.child_problems(rows, mark=Mark(3, 30), rates=RATES))

    def test_a_tick_is_bounded_by_its_resources_at_the_rates_of_the_peer(self):
        # 1 GiB and 1 vCPU for 10 s: (101 + 1001) * 10 * 2 = 22040.
        self.assertEqual(check.maintenance_bound(tick(0, 1).detail, RATES), 22040)
        self.assertEqual(check.child_problems([tick(-22040, 4)], mark=Mark(3, 0), rates=RATES), [])
        self.assertTrue(check.child_problems([tick(-22041, 4)], mark=Mark(3, 0), rates=RATES))

    def test_a_tick_that_names_no_resources_is_not_bounded(self):
        row = Row(4, 140, -10 ** 9, 0, ledger.MAINTENANCE, "", {"peer_mu": "1"})
        self.assertEqual(check.child_problems([row], mark=Mark(3, 0), rates=RATES), [])

    def test_ticks_cannot_charge_more_time_than_passed(self):
        rows = [tick(-1, seq, ts=200) for seq in (4, 5, 6)]

        self.assertEqual(check.child_problems(rows[:2], mark=Mark(3, 0, ts=190), rates=RATES), [])
        self.assertTrue(check.child_problems(rows, mark=Mark(3, 0, ts=190), rates=RATES))

    def test_what_the_child_does_with_its_own_balance_is_not_a_charge_of_the_peer(self):
        rows = [Row(4, 140, -50, 0, kind, "") for kind in (
            ledger.CHILD_START, ledger.TOP_UP, ledger.TUNNEL, ledger.RESOURCE_CHANGE,
            ledger.STATEMENT_QUERY,
        )]
        self.assertEqual(check.child_problems(rows, mark=Mark(3, 0), rates=RATES), [])

    def test_an_unknown_charge_is_an_overcharge(self):
        rows = [Row(4, 140, -1, 0, "fee", ""), Row(5, 140, -1, 0, ledger.DEBIT, "")]
        self.assertEqual(len(check.child_problems(rows, mark=Mark(3, 0), rates=RATES)), 2)

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

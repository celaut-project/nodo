"""A peer that changes a delegated instance in a way its quote did not say loses reputation (#344, 7)."""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.database import ledger
    from src.manager import closing_check, maintain, statement_check
    from src.manager.statement_check import Row
    from src.reputation_system.reasons import Reason
    from src.utils import dead_instances
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

T = 1_800_000_000          # the close that the quote named
BEFORE = T - 3600
AFTER = T + 30


def _maintenance(seq, ts, mem):
    return Row(seq=seq, ts=ts, amount=-10, balance_after=1000, kind=ledger.MAINTENANCE,
               ref="", detail={"seconds": "10", "mem_limit": str(mem)})


def _stop(seq, ts, reason=dead_instances.ACTIVITY_WINDOW):
    return Row(seq=seq, ts=ts, amount=0, balance_after=900, kind=ledger.STOP, ref="",
               detail={"reason": reason, "leftover": "900"})


def _resize(seq, ts):
    return Row(seq=seq, ts=ts, amount=-1, balance_after=1000, kind=ledger.RESOURCE_CHANGE,
               ref="", detail={})


def _hibernation_at(ts):
    return [_maintenance(1, ts - 20, 512), _maintenance(2, ts, 0)]


def _reasons(faults):
    return sorted(f.reason for f in faults)


HIB = Reason.INSTANCE_HIBERNATED_UNANNOUNCED
STO = Reason.INSTANCE_STOPPED_UNANNOUNCED


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EveryCellOfTheTableTests(unittest.TestCase):
    def judge(self, rows, on_close, closes_at, previous_mem=512):
        return closing_check.judge(rows, on_close, closes_at, previous_mem)

    # REFUSE, or closes_at 0
    def test_refuse_hibernation_is_unannounced(self):
        self.assertEqual(_reasons(self.judge(_hibernation_at(AFTER), "refuse", T)), [HIB])

    def test_refuse_stop_is_unannounced(self):
        self.assertEqual(_reasons(self.judge([_stop(3, AFTER)], "refuse", T)), [STO])

    def test_no_close_scheduled_hibernation_and_stop_are_unannounced(self):
        self.assertEqual(_reasons(self.judge(_hibernation_at(AFTER), "hibernate", 0)), [HIB])
        self.assertEqual(_reasons(self.judge([_stop(3, AFTER)], "stop", 0)), [STO])

    # HIBERNATE at T
    def test_hibernate_quote_hibernation_before_t_is_unannounced(self):
        self.assertEqual(_reasons(self.judge(_hibernation_at(BEFORE), "hibernate", T)), [HIB])

    def test_hibernate_quote_hibernation_at_or_after_t_is_expected(self):
        self.assertEqual(self.judge(_hibernation_at(AFTER), "hibernate", T), [])
        self.assertEqual(self.judge(_hibernation_at(T), "hibernate", T), [])

    def test_hibernate_quote_stop_is_unannounced_at_any_time(self):
        for ts in (BEFORE, AFTER):
            self.assertEqual(_reasons(self.judge([_stop(3, ts)], "hibernate", T)), [STO])

    # STOP at T
    def test_stop_quote_hibernation_before_t_is_unannounced(self):
        self.assertEqual(_reasons(self.judge(_hibernation_at(BEFORE), "stop", T)), [HIB])

    def test_stop_quote_hibernation_from_t_is_milder_than_the_quote_and_not_scored(self):
        self.assertEqual(self.judge(_hibernation_at(AFTER), "stop", T), [])

    def test_stop_quote_stop_before_t_is_unannounced(self):
        self.assertEqual(_reasons(self.judge([_stop(3, BEFORE)], "stop", T)), [STO])

    def test_stop_quote_stop_from_t_is_expected(self):
        self.assertEqual(self.judge([_stop(3, AFTER)], "stop", T), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WhatIsNotScoredTests(unittest.TestCase):
    def test_a_stop_costs_more_than_a_hibernation(self):
        self.assertLess(closing_check.STOPPED_UNANNOUNCED_PENALTY,
                        closing_check.HIBERNATED_UNANNOUNCED_PENALTY)
        self.assertLess(closing_check.STOPPED_UNANNOUNCED_PENALTY, -100)  # INSTANCE_LOST
        self.assertGreater(closing_check.HIBERNATED_UNANNOUNCED_PENALTY, -100)

    def test_a_peer_that_ran_out_of_balance_scores_nothing(self):
        rows = [_stop(3, BEFORE, dead_instances.OUT_OF_BALANCE),
                _stop(4, BEFORE, dead_instances.HIBERNATED_OUT_OF_BALANCE)]
        self.assertEqual(closing_check.judge(rows, "refuse", 0), [])

    def test_failures_and_manual_stops_are_not_stops_of_the_hours(self):
        for reason in (dead_instances.PROCESS_LOST, dead_instances.GUEST_PANIC,
                       dead_instances.MANUAL_KILL, dead_instances.PEER_STOPPED):
            self.assertEqual(closing_check.judge([_stop(3, BEFORE, reason)], "refuse", 0), [], reason)

    def test_a_hibernation_that_the_instance_asked_for_is_not_the_peers(self):
        rows = [_maintenance(1, BEFORE - 20, 512), _resize(2, BEFORE - 10),
                _maintenance(3, BEFORE, 0)]
        self.assertEqual(closing_check.judge(rows, "refuse", 0, previous_mem=512), [])

    def test_a_long_hibernation_is_one_event_not_one_per_row(self):
        rows = _hibernation_at(AFTER) + [_maintenance(3, AFTER + 10, 0), _maintenance(4, AFTER + 20, 0)]
        faults = closing_check.judge(rows, "refuse", 0, previous_mem=512)
        self.assertEqual([f.seq for f in faults], [2])

    def test_an_instance_already_hibernated_before_these_rows_is_not_a_new_event(self):
        rows = [_maintenance(5, AFTER, 0), _maintenance(6, AFTER + 10, 0)]
        self.assertEqual(closing_check.judge(rows, "refuse", 0, previous_mem=0), [])

    def test_when_the_previous_memory_is_unknown_the_first_row_is_not_a_change(self):
        rows = [_maintenance(5, AFTER, 0)]
        self.assertEqual(closing_check.judge(rows, "refuse", 0, previous_mem=None), [])

    def test_a_row_with_no_quote_is_not_judged(self):
        self.assertEqual(closing_check.judge(_hibernation_at(AFTER) + [_stop(3, AFTER)], None, None), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CheckScoresThePeerTests(unittest.TestCase):
    ROW = {"peer_id": "peer-a", "token": "tok", "id": "alias", "mem_limit": 512,
           "quote_on_close": "refuse", "quote_closes_at": 0}

    def test_each_fault_is_scored_to_the_peer_with_its_own_weight(self):
        rows = _hibernation_at(AFTER) + [_stop(3, AFTER + 60)]
        with mock.patch.object(statement_check, "penalise_once") as penalise:
            closing_check.check(self.ROW, rows)
        scored = {call.args[2]: call.kwargs["amount"] for call in penalise.call_args_list}
        self.assertEqual(scored, {
            HIB: closing_check.HIBERNATED_UNANNOUNCED_PENALTY,
            STO: closing_check.STOPPED_UNANNOUNCED_PENALTY,
        })
        for call in penalise.call_args_list:
            self.assertEqual(call.args[0], "peer-a")

    def test_what_the_quote_said_costs_nothing(self):
        row = dict(self.ROW, quote_on_close="stop", quote_closes_at=T)
        with mock.patch.object(statement_check, "penalise_once") as penalise:
            closing_check.check(row, _hibernation_at(AFTER) + [_stop(3, AFTER)])
        penalise.assert_not_called()

    def test_a_check_that_fails_does_not_stop_the_tick(self):
        with mock.patch.object(closing_check, "judge", side_effect=RuntimeError("x")):
            closing_check.check(self.ROW, [])

    def test_the_reasons_are_stable_strings(self):
        self.assertEqual(HIB, "instance_hibernated_unannounced")
        self.assertEqual(STO, "instance_stopped_unannounced")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReadingAHibernatedChildTests(unittest.TestCase):
    def test_a_hibernated_child_is_read_as_held_and_not_called_peer_stopped(self):
        """A peer that hibernated the child still answers `Metrics` for it, so the read
        succeeds and the delegating node never reaches the `PEER_STOPPED` branch."""
        row = {"token": "tok", "id": "alias", "peer_id": "peer-a",
               "peer_balance_mu": 1000, "launched_ts": 1}
        metrics = mock.Mock(balance=mock.Mock(n="900"), suspended=False)
        with mock.patch.object(maintain, "instance_metrics_on_peer", return_value=metrics), \
                mock.patch.object(maintain.sc, "child_statement_due", return_value=False):
            charge, state = maintain._read_child(dict(row, peer_statement_seq=1,
                                                      peer_statement_hash="h",
                                                      peer_statement_ts=1))
        self.assertFalse(state["peer_suspended"])


if __name__ == "__main__":
    unittest.main()

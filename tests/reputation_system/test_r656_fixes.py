"""The bugs of the review of PR #656 (off-chain reputation, #652), fixed.

The reviewer wrote these as repros of head dea12bec, each asserting the buggy behaviour.
Here each asserts the fixed one (B1 to B6 and the nit N1). B1 uses the real SQL and signing
path, and the other tests build pages the way the node code writes them.
"""
import time
import unittest
from unittest.mock import patch

from tests.reputation_system.test_offchain_reputation import (
    IMPORT_ERROR, NOW, _Db, _Signer, _chain, _evidence, _page, _proof, _quote, _tick,
)

if IMPORT_ERROR is None:
    from protos import celaut_pb2
    from src.database import ledger
    from src.identity import node_identity, statement_signature
    from src.manager import balance_statement, maintain, statement_check
    from src.reputation_system import fault_proofs, offchain_proof
    from src.reputation_system.reasons import Reason
    from src.utils.cost_functions import start_quote

BASE = {"mem_limit": 512 * 2**20, "disk_space": 2**30, "cpu_period": 100000, "cpu_quota": 100000}
MOST = {"mem_limit": 2048 * 2**20, "disk_space": 2**30, "cpu_period": 100000, "cpu_quota": 200000}


def _shape_row():
    """A delegated row as ``priced_shape`` writes it (base = start, max = most)."""
    row = dict(BASE)
    for key in BASE:
        row[f"base_{key}"] = BASE[key]
        row[f"max_{key}"] = MOST[key]
    return row


def _signed_movements(account, rows, start_ts):
    """``rows`` are ``(dt, amount, kind, detail)``; dt is seconds after start_ts."""
    prev = ledger.genesis_hash(account)
    balance, out = 0, []
    for i, (dt, amount, kind, detail) in enumerate(rows, start=1):
        balance += amount
        text = ledger.canonical_detail(detail) or ""
        m = celaut_pb2.BalanceMovement(
            seq=i, ts=start_ts + dt, amount=celaut_pb2.Amount(n=str(amount)),
            balance_after=celaut_pb2.Amount(n=str(balance)), kind=kind, detail=text,
            prev_hash=prev)
        out.append(m)
        prev = statement_signature.movement_hash(m)
    return out


def _sub_page(signer, account, movements, head, ts=NOW):
    return _page(signer, account, movements, ts=ts, head=head)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class B1HeadZeroAfterRetention(_Db, unittest.TestCase):
    """An honest node whose retention deleted a dead account signs head_seq 0. With any
    earlier page of the same account, check() proves 'the head went back'."""

    def test_real_answer_then_prune_proves_inconsistency(self):
        node = _Signer()
        with node:
            self.sql.add_client("father", 10**12, None)
            self.sql.add_local_instance(
                father_id="father", container_ip="10.0.0.2", container_id="inst", name="i",
                balance_mu=1000, serialized_instance="", service_id="ab" * 32,
                virtualizer="ch", disk_space=0, envs="",
                movements=(ledger.Movement(ledger.START, 1000),))
            self.sql.move_balance("inst", -10, ledger.MAINTENANCE, detail=_tick())
            early = balance_statement._answer_account("father", "inst", 1, 0, False,
                                                      with_dead=True)
            self.assertGreaterEqual(early.head_seq, 2)
            time.sleep(1.1)
            # The instance stops; its rows age past the retention; the sweep deletes them.
            self.connection.execute(
                "UPDATE local_instances SET killed_at = CURRENT_TIMESTAMP WHERE id = 'inst'")
            self.connection.execute(
                "UPDATE balance_movements SET ts = ts - 400 * 86400 WHERE account_id = 'inst'")
            self.connection.commit()
            self.assertGreater(self.sql.prune_balance_movements(30), 0)
            late = balance_statement._answer_account("father", "inst", 1, 0, False,
                                                     with_dead=True)
        self.assertEqual(late.head_seq, 0)
        self.assertEqual(len(late.movements), 0)
        # head_conflict (#632) calls this "not a conflict"; fault_proofs calls it a proof.
        proven = fault_proofs.check(_evidence(
            Reason.STATEMENT_INCONSISTENT, node.peer_id, [early, late]))
        self.assertIsNone(proven, "an honest pruned account is not framed")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class B2RelayedStatementAccusesTheRelayer(unittest.TestCase):
    """A relayed statement is the far node's rows re-signed by the relayer (_relay). As
    evidence, check() proves the far node's fault against the relayer."""

    def test_relayed_killed_with_mu_is_proven_against_the_relayer(self):
        relayer = _Signer()
        far_rows = _chain("d", [(100, "start", None),
                                (-80, "refund", {"lost": "father_gone"}),
                                (0, "stop", {"reason": "process_lost", "leftover": 80})])
        page = _page(relayer, "d", far_rows, instance_ref="ef" * 32)
        page.relayed = True
        with relayer:  # what balance_statement._relay does
            page.signature = node_identity.sign_peer_payload(node_identity.statement_payload(
                relayer.peer_id, statement_signature.statement_digest(page)))
        proven = fault_proofs.check(_evidence(Reason.KILLED_WITH_MU, relayer.peer_id, [page]))
        self.assertIsNone(proven, "a relayed page proves nothing against the relayer")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class B3SignedQuoteOverridesOffer(_Db, unittest.TestCase):
    """The quote signed in the start movement REPLACES the EstimatedCost the peer was
    chosen on, even when it is worse for us. A peer quotes bounded/low, signs
    unbounded/high, and is never scored for maintenance_overcharged."""

    def test_unbounded_signed_quote_disables_the_local_check(self):
        from src.gateway.launcher.delegate_execution import delegate_execution as de

        self.connection.execute(
            "INSERT INTO delegated_instances (id, token_delegation, peer_id) "
            "VALUES ('alias', 'tok', 'peer')")
        self.connection.commit()
        offered = celaut_pb2.EstimatedCost(
            init_maintenance_cost=celaut_pb2.Amount(n="10"),
            max_maintenance_cost=celaut_pb2.Amount(n="20"),
            maintenance_seconds_loop=10, price_bounded=True, price_volatility=0.0)
        start_row = statement_check.rows_of(celaut_pb2.BalanceStatement(movements=_chain(
            "c", [(1000, "start", _quote(init=1000, most=5000, bounded=False))])))[0]
        columns = statement_check.signed_quote_row(start_row)
        de._keep_signed_start("peer", "tok", offered, columns, b"page")
        row = dict(self.connection.execute(
            "SELECT * FROM delegated_instances WHERE token_delegation = 'tok'").fetchone())
        # The signed quote does not loosen the offer: still bounded, at the offered figures.
        self.assertEqual(int(row["quote_price_bounded"]), 1)
        self.assertEqual(int(row["quote_max_maintenance_cost"]), 20)
        self.assertEqual(int(row["quote_init_maintenance_cost"]), 10)
        quote = statement_check.MaintenanceQuote.of_row(row)
        self.assertIsNotNone(quote, "the check against the peer stays on")
        # The charges of the e2e: far above the offer, and now an overcharge locally.
        charges = statement_check.rows_of(celaut_pb2.BalanceStatement(movements=_chain(
            "c", [(1000, "start", None), (-500, "maintenance", _tick(seconds=10))])))
        self.assertIsNotNone(statement_check.maintenance_overcharge(
            charges[1:], quote, charges[0].ts))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class B5LaggedLocalTickTruncation(unittest.TestCase):
    """Each resource term of a charge is truncated (per_time_charge). The quote figure
    is the truncated price of ONE loop, and maintenance_overcharge allows only +1 MU per
    row. A local tick that lags (seconds up to 6 loops, _seconds_to_charge) at a factor
    of 1 (PRICE_VOLATILITY 0, or the price at its bound) charges more than
    figure * seconds / loop + 1: an honest single page proves an overcharge."""

    def test_honest_60s_tick_at_launch_price_is_proven(self):
        from src.utils.cost_functions.execution_cost import charge_at_launch_price
        from src.utils.cost_functions.price_bound import PriceBound

        node = _Signer()
        start = celaut_pb2.Sysresources(**BASE)
        most = celaut_pb2.Sysresources(**MOST)
        terms = start_quote.local_terms(start, most, maintain.BASE_LAUNCH_BP,
                                        PriceBound(0.0, 86400), None, "refuse", 0)
        loop = terms["maintenance_seconds_loop"]
        lag = 6 * loop
        honest = charge_at_launch_price(start, lag, maintain.BASE_LAUNCH_BP)
        print(f"\nfigure {terms['init_maintenance_cost']} per {loop}s; "
              f"honest {lag}s tick {honest}; allowed {terms['init_maintenance_cost'] * 6 + 1}")
        movements = _signed_movements("inst", [
            (0, 10**9, ledger.START, statement_check.start_quote_detail(
                terms, start_quote.shape_of(start))),
            (lag, -honest, ledger.MAINTENANCE, statement_check.tick_detail(BASE, lag)),
        ], NOW - 1000)
        page = _page(node, "inst", movements)
        proven = fault_proofs.check(_evidence(Reason.MAINTENANCE_OVERCHARGED, node.peer_id, [page]))
        self.assertIsNone(proven, "an honest lagged tick is not an overcharge")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class B6HibernateRefusedThenStop(unittest.TestCase):
    """local_terms quotes on_close = the node policy ('hibernate'). An instance that
    cannot hibernate (shares, no revivable disk) is STOPPED at closing time with reason
    activity_window (_hibernate_for_window). closing_check says: hibernate quote + stop
    at any time = unannounced. The honest node proves its own fault."""

    def test_policy_stop_is_proven_stopped_unannounced(self):
        node = _Signer()
        closes_at = NOW - 500
        movements = _signed_movements("inst", [
            (0, 1000, ledger.START, _quote(on_close="hibernate", closes_at=closes_at)),
            (10, -10, ledger.MAINTENANCE, _tick()),
            (600, 0, ledger.STOP, {"reason": "activity_window", "leftover": 990}),
        ], NOW - 1100)
        page = _page(node, "inst", movements)
        proven = fault_proofs.check(_evidence(
            Reason.INSTANCE_STOPPED_UNANNOUNCED, node.peer_id, [page]))
        self.assertIsNone(proven, "the stop of an instance that cannot hibernate is no fault")
        early = _signed_movements("inst", [
            (0, 1000, ledger.START, _quote(on_close="hibernate", closes_at=closes_at)),
            (10, -10, ledger.MAINTENANCE, _tick()),
            (100, 0, ledger.STOP, {"reason": "activity_window", "leftover": 990}),
        ], NOW - 1100)
        proven = fault_proofs.check(_evidence(
            Reason.INSTANCE_STOPPED_UNANNOUNCED, node.peer_id,
            [_page(node, "inst", early)]))
        self.assertIsNotNone(proven, "a stop before the close is still a fault")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class B7OwnFaultWithoutEvidenceIsScoredAgain(_Db, unittest.TestCase):
    """A fault this node scored itself with no evidence (statement_faults only) is scored
    a second time when the same fault arrives with evidence in a proof."""

    def test_double_penalty(self):
        me, accused = _Signer(), _Signer()
        self.add_peer(accused.peer_id)
        page = _page(accused, "c", _chain("c", [
            (100, "start", None), (-80, "refund", {"lost": "father_gone"}),
            (0, "stop", {"reason": "process_lost", "leftover": 80})]))
        with patch("src.reputation_system.interface.update_peer_reputation") as scored:
            statement_check.penalise_once(accused.peer_id, "c", Reason.KILLED_WITH_MU,
                                          "seen here", 3)
            offchain_proof.apply_evidence(
                _evidence(Reason.KILLED_WITH_MU, accused.peer_id, [page]),
                me.peer_id, {accused.peer_id})
        self.assertEqual(scored.call_count, 1, "the fault is scored once")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class N1VerifyRaises(_Db, unittest.TestCase):
    """A malformed Amount.n inside an evidence is refused by verify()/receive(): it does not
    raise, so the rest of a pull is not lost."""

    def test_verify_raises_value_error(self):
        accused = _Signer()
        page = _page(accused, "c", _chain("c", [(100, "start", None)]))
        page.movements[0].amount.n = "not-a-number"
        opinion = celaut_pb2.ReputationOpinion(type="x", object=accused.peer_id)
        opinion.evidence.CopyFrom(_evidence(Reason.KILLED_WITH_MU, accused.peer_id, [page]))
        proof = celaut_pb2.ReputationProof(publisher=accused.peer_id, ts=NOW, signature="00")
        proof.opinions.append(opinion)
        from src.identity import execution_receipt
        proof.signature_scheme.CopyFrom(execution_receipt.receipt_scheme())
        self.assertFalse(offchain_proof.verify(proof))
        self.assertEqual(offchain_proof.receive(proof.SerializeToString()),
                         "not signed by its publisher")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StricterQuoteTests(unittest.TestCase):
    """``stricter_quote``: the signed quote never loosens the offer (B3)."""

    OFFER = {"init_maintenance_cost": 10, "max_maintenance_cost": 20,
             "maintenance_seconds_loop": 10, "price_bounded": True, "price_volatility": 0.1,
             "volatility_period_seconds": 3600, "on_close": "stop", "closes_at": 5000}

    def _signed(self, **over):
        row = {"quote_init_maintenance_cost": 10, "quote_max_maintenance_cost": 20,
               "quote_maintenance_seconds_loop": 10, "quote_price_bounded": 1,
               "quote_price_volatility": 0.1, "quote_volatility_period_seconds": 3600,
               "quote_on_close": "stop", "quote_closes_at": 5000, "quote_launch_ts": NOW}
        row.update(over)
        return row

    def stricter(self, offered, signed):
        from src.gateway.launcher.delegate_execution.delegate_execution import stricter_quote
        return stricter_quote(offered, signed)

    def test_an_honest_quote_is_kept_as_signed(self):
        columns, notes = self.stricter(self.OFFER, self._signed())
        self.assertEqual(columns, self._signed())
        self.assertEqual(notes, [])

    def test_no_offer_or_an_unbounded_offer_keeps_the_signed_quote(self):
        signed = self._signed(quote_price_bounded=0)
        self.assertEqual(self.stricter(None, signed)[0], signed)
        self.assertEqual(self.stricter({**self.OFFER, "price_bounded": False}, signed)[0], signed)

    def test_a_figure_above_the_room_for_the_price_is_held_to_it(self):
        columns, notes = self.stricter(self.OFFER, self._signed(
            quote_init_maintenance_cost=11, quote_max_maintenance_cost=5000))
        self.assertEqual(columns["quote_init_maintenance_cost"], 11)   # 10 * 1.1
        self.assertEqual(columns["quote_max_maintenance_cost"], 22)    # 20 * 1.1
        self.assertEqual(len(notes), 1)

    def test_a_lower_signed_figure_is_kept(self):
        columns, _ = self.stricter(self.OFFER, self._signed(quote_max_maintenance_cost=15))
        self.assertEqual(columns["quote_max_maintenance_cost"], 15)

    def test_unbounded_or_a_larger_volatility_does_not_loosen_the_bound(self):
        columns, notes = self.stricter(self.OFFER, self._signed(
            quote_price_bounded=0, quote_price_volatility=None,
            quote_volatility_period_seconds=None))
        self.assertEqual((columns["quote_price_bounded"], columns["quote_price_volatility"]),
                         (1, 0.1))
        self.assertTrue(notes)
        columns, _ = self.stricter(self.OFFER, self._signed(quote_price_volatility=0.9))
        self.assertEqual(columns["quote_price_volatility"], 0.1)
        columns, _ = self.stricter(self.OFFER, self._signed(quote_price_volatility=0.05))
        self.assertEqual(columns["quote_price_volatility"], 0.05)

    def test_an_earlier_close_than_the_offer_does_not_replace_it(self):
        columns, notes = self.stricter(self.OFFER, self._signed(
            quote_on_close="refuse", quote_closes_at=100))
        self.assertEqual((columns["quote_on_close"], columns["quote_closes_at"]), ("stop", 5000))
        self.assertTrue(notes)

    def test_another_loop_length_keeps_the_offered_terms(self):
        columns, _ = self.stricter(self.OFFER, self._signed(
            quote_maintenance_seconds_loop=1000, quote_max_maintenance_cost=5000))
        self.assertEqual(columns["quote_maintenance_seconds_loop"], 10)
        self.assertEqual(columns["quote_max_maintenance_cost"], 20)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OverchargeStillCaughtTests(unittest.TestCase):
    """The room for truncation (B5) is 3 MU a loop, not a free pass."""

    def test_double_the_price_is_still_an_overcharge(self):
        node = _Signer()
        start = celaut_pb2.Sysresources(**BASE)
        from src.utils.cost_functions.execution_cost import charge_at_launch_price
        from src.utils.cost_functions.price_bound import PriceBound

        terms = start_quote.local_terms(start, celaut_pb2.Sysresources(**MOST),
                                        maintain.BASE_LAUNCH_BP, PriceBound(0.0, 86400),
                                        None, "refuse", 0)
        loop = terms["maintenance_seconds_loop"]
        honest = charge_at_launch_price(start, 6 * loop, maintain.BASE_LAUNCH_BP)
        movements = _signed_movements("inst", [
            (0, 10**9, ledger.START, statement_check.start_quote_detail(
                terms, start_quote.shape_of(start))),
            (6 * loop, -2 * honest, ledger.MAINTENANCE,
             statement_check.tick_detail(BASE, 6 * loop)),
        ], NOW - 1000)
        proven = fault_proofs.check(_evidence(
            Reason.MAINTENANCE_OVERCHARGED, node.peer_id, [_page(node, "inst", movements)]))
        self.assertIsNotNone(proven)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SignatureCaseTests(unittest.TestCase):

    def test_an_uppercase_signature_is_refused(self):
        signer = _Signer()
        proof = _proof(signer, [celaut_pb2.ReputationOpinion(
            type="x", object="ab" * 32, amount=1, positive=True)])
        self.assertTrue(offchain_proof.verify(proof))
        proof.signature = proof.signature.upper()
        self.assertFalse(offchain_proof.verify(proof))

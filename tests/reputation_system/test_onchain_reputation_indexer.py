"""Filling the on-chain index: what goes into it, and what a failure does.

The index is the only thing standing between an explorer and a routing decision, so what
is pinned here is the boundary rather than the arithmetic (that is
``test_onchain_reputation_weight``):

* **every** publishing proof is stored, attributable to a known peer or not -- a proof
  earns its voice by what it says, scored at read time, and filtering by who owns it
  would only exclude the publishers an attacker does not need;
* the burn behind each proof reaches the row, because the reader prices it;
* a publisher's boxes are netted per proof before they are written, so splitting a stake
  into ten boxes does not speak ten times;
* the subject's own proofs are set aside first (issue #351);
* an unreachable explorer leaves the stored rows alone rather than blanking a peer;
* and nothing here is on the routing path -- the tick is hourly and self-gating.

Issue #353.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.reputation_system import onchain_indexer
    from src.reputation_system.opinions import Opinion
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def opinion(proof, amount=1, assigned=4, positive=True, box="b"):
    return Opinion(
        ledger="ergo",
        proof_id=proof,
        owner="0008cd" + "02" * 33,
        amount=amount,
        assigned_amount=assigned,
        positive=positive,
        published_at=0,
        box_id=box,
        # A large sacrifice on every opinion, so any test that passed *because* of the
        # burn would pass loudly rather than subtly.
        burned_nanoerg=10 ** 15,
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OpinionReadingTests(unittest.TestCase):

    def _read(self, collected, own=()):
        with mock.patch(
            "src.reputation_system.interface._opinion_readers",
            return_value={"ergo": lambda node_id: collected},
        ), mock.patch(
            "src.reputation_system.interface._own_proof_ids", return_value=tuple(own)
        ):
            return onchain_indexer.opinions_for("peer-a")

    def test_a_proof_nobody_here_has_heard_of_is_stored_like_any_other(self):
        """Where this parts from the version that filtered by ownership.

        Storing it costs a row and decides nothing: what the proof is worth is settled at
        read time, by whether its opinions match ours. Filtering here would have excluded
        exactly the publishers an attacker does not need -- minting a proof is free, so
        an ownership test is one the attack passes and an honest newcomer fails.
        """
        rows = self._read([opinion("stranger-proof")])
        self.assertEqual([proof for proof, _, _ in rows], ["stranger-proof"])

    def test_the_burn_behind_the_proof_reaches_the_row(self):
        """The reader prices the burn, so the indexer has to carry it."""
        rows = self._read([opinion("proof-1")])
        self.assertEqual(rows[0][2], 10 ** 15)

    def test_the_burn_is_the_proofs_and_is_not_multiplied_by_its_boxes(self):
        """A property of the proof, not of a box: two boxes are one sacrifice."""
        rows = self._read([opinion("proof-1", box="b1"), opinion("proof-1", box="b2")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 10 ** 15)

    def test_a_publishers_boxes_are_netted_before_the_row_is_written(self):
        """One proof speaks once, however many boxes it split its stake into."""
        rows = self._read(
            [opinion("proof-1", amount=1, box="b1"), opinion("proof-1", amount=1, box="b2")]
        )
        self.assertEqual(len(rows), 1)
        proof_id, verdict, _burned = rows[0]
        self.assertEqual(proof_id, "proof-1")
        self.assertAlmostEqual(verdict, 0.5)

    def test_polarity_survives_into_the_row(self):
        rows = self._read([opinion("proof-1", positive=False)])
        self.assertLess(rows[0][1], 0)

    def test_a_proof_that_cancels_itself_out_says_nothing(self):
        rows = self._read([
            opinion("proof-1", positive=True, box="b1"),
            opinion("proof-1", positive=False, box="b2"),
        ])
        self.assertEqual(rows[0][1], 0.0)

    def test_the_subjects_own_proofs_never_reach_the_index(self):
        rows = self._read(
            [opinion("own-proof"), opinion("other-proof")], own=("own-proof",)
        )
        self.assertEqual([proof for proof, _, _ in rows], ["other-proof"])

    def test_an_explorer_failure_raises_rather_than_reporting_no_opinion(self):
        """An empty list is a verdict. A failed read must not be able to give it."""
        def boom(node_id):
            raise RuntimeError("explorer unreachable")

        with mock.patch(
            "src.reputation_system.interface._opinion_readers",
            return_value={"ergo": boom},
        ), mock.patch(
            "src.reputation_system.interface._own_proof_ids", return_value=()
        ):
            with self.assertRaises(RuntimeError):
                onchain_indexer.opinions_for("peer-a")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RefreshTests(unittest.TestCase):

    def test_an_unreadable_peer_leaves_its_stored_rows_alone(self):
        """The chain said something last hour; an unreachable explorer is not a retraction.

        Note the asymmetry with the routing path: *there*, a failure zeroes every
        candidate. *Here*, a failure changes nothing at all. Both refuse to let one
        unreadable thing invent a verdict.
        """
        replaced = []

        def opinions_for(peer_id):
            if peer_id == "peer-b":
                raise RuntimeError("explorer unreachable")
            return [("proof-1", 0.25, 10 ** 15)]

        with mock.patch.object(
            onchain_indexer, "opinions_for", side_effect=opinions_for
        ), mock.patch(
            "src.database.sql_connection.SQLConnection.get_peers_id",
            return_value=["peer-a", "peer-b"],
        ), mock.patch(
            "src.database.sql_connection.SQLConnection.replace_onchain_opinions",
            side_effect=lambda ledger, subject, rows: replaced.append(subject) or True,
        ):
            written = onchain_indexer.refresh()

        self.assertEqual(replaced, ["peer-a"])
        self.assertEqual(written, 1)

    def test_a_node_that_knows_nobody_does_no_work_and_raises_nothing(self):
        # What a young node looks like. Not an error: there is simply no subject to ask
        # the chain about yet.
        with mock.patch(
            "src.database.sql_connection.SQLConnection.get_peers_id", return_value=[]
        ):
            self.assertEqual(onchain_indexer.refresh(), 0)

    def test_the_tick_is_hourly_self_gating_and_never_raises(self):
        onchain_indexer._last_refresh = None
        calls = []

        with mock.patch.object(
            onchain_indexer, "refresh", side_effect=lambda: calls.append(1) or 0
        ):
            onchain_indexer.tick()
            onchain_indexer.tick()
        self.assertEqual(len(calls), 1, "the second call is inside the interval")

        onchain_indexer._last_refresh = None
        with mock.patch.object(
            onchain_indexer, "refresh", side_effect=RuntimeError("boom")
        ):
            onchain_indexer.tick()  # must not propagate into the maintenance loop

        onchain_indexer._last_refresh = None

    def test_the_refresh_interval_keeps_the_explorer_clear_of_a_launch(self):
        self.assertGreaterEqual(onchain_indexer.REFRESH_INTERVAL_SECONDS, 3600)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NoNetworkOnTheRoutingPathTests(unittest.TestCase):
    """The constraint the issue calls non-negotiable, asserted structurally.

    An explorer read inside ``estimated_cost_sorter`` is not acceptable, so the sorter
    must reach the chain through nothing but a SQLite read. Checked against the *parsed
    imports* rather than against behaviour or raw text: a mock that happened to answer
    would hide exactly the regression this exists to catch, and a substring match would
    fire on a prose comment saying the read is not done.
    """

    # What a routing-path module would have to import to reach a chain -- the HTTP
    # clients, and every module in the tree that uses one.
    FORBIDDEN = (
        "requests",
        "urllib",
        "urllib.request",
        "src.reputation_system.contracts.ergo.opinions",
        "src.reputation_system.contracts.ergo.utils",
        "src.reputation_system.onchain_indexer",
        "src.payment_system.donations.indexer",
    )

    @staticmethod
    def _imports(module):
        """Every module name ``module`` imports, at module scope or inside a function."""
        import ast
        import inspect

        names = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    def _assert_no_chain_reader(self, module):
        imported = self._imports(module)
        for forbidden in self.FORBIDDEN:
            self.assertNotIn(
                forbidden, imported,
                f"{module.__name__} must not import {forbidden}: it is on the routing path",
            )

    def test_the_sorter_reaches_no_chain(self):
        from src.balancers.estimated_cost_sorter import estimated_cost_sorter as sorter

        self._assert_no_chain_reader(sorter)

    def test_the_credit_module_reaches_no_chain(self):
        from src.reputation_system import onchain_credit

        self._assert_no_chain_reader(onchain_credit)

    def test_the_scoring_arithmetic_imports_nothing_at_all_from_the_tree(self):
        # Pure arithmetic: no database, no config, no network, as its docstring says.
        from src.balancers import scoring

        self.assertFalse({name for name in self._imports(scoring) if name.startswith("src.")})


if __name__ == "__main__":
    unittest.main()

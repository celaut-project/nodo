"""Filling the on-chain index: who is allowed to publish into it, and what a failure does.

The index is the only thing standing between an explorer and a routing decision, so what
is pinned here is the boundary rather than the arithmetic (that is
``test_onchain_reputation_weight``):

* a proof is attributable **only** when a peer announced it *and* signed an owner
  attestation for it -- the same two links ``nodo verify_reputation`` prints;
* a proof announced by two peers is attributed to neither;
* an opinion from an unattributable proof is dropped, never stored unattributed;
* a publisher's boxes are netted per proof before they are written, so splitting a stake
  into ten boxes does not speak ten times;
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


class _Contract:
    """The two things the indexer reads off an announced proof."""

    def __init__(self, token_id):
        self.token_id = token_id


class _Announcement:
    def __init__(self, proofs):
        self.reputation_proofs = proofs
        self._parsed = False

    def ParseFromString(self, blob):
        self._parsed = True


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PublisherAttributionTests(unittest.TestCase):

    def _publishers(self, peers, attested, token_ids):
        """Run ``publisher_peers`` against a stubbed peer table.

        ``peers`` maps peer id -> the proof ids in its advertisement; ``attested`` is the
        set of (peer id, proof id) pairs whose owner attestation verifies.
        """
        advertisements = {
            peer_id: _Announcement([_Contract(t) for t in proofs])
            for peer_id, proofs in peers.items()
        }

        def parse(blob):
            return advertisements[blob]

        with mock.patch(
            "src.database.sql_connection.SQLConnection.get_peers_id",
            return_value=list(peers),
        ), mock.patch(
            "src.database.sql_connection.SQLConnection.get_peer_advertisement",
            side_effect=lambda peer_id: peer_id,
        ), mock.patch(
            "protos.celaut_pb2.Peer", side_effect=lambda: _Parsed(advertisements)
        ), mock.patch(
            "src.utils.contract_xattrs.get_token_id", side_effect=token_ids
        ), mock.patch(
            "src.reputation_system.proof_attestation.attested_proof_owner",
            side_effect=lambda contract, peer_id: (
                "0" * 66 if (peer_id, contract.token_id) in attested else None
            ),
        ):
            return onchain_indexer.publisher_peers()

    def test_an_announced_and_attested_proof_is_attributed_to_its_peer(self):
        result = self._publishers(
            {"peer-a": ["proof-1"]},
            attested={("peer-a", "proof-1")},
            token_ids=lambda c: c.token_id,
        )
        self.assertEqual(result, {"proof-1": "peer-a"})

    def test_an_announced_proof_with_no_provable_owner_is_not_attributed(self):
        """Naming someone else's wallet is free; signing with it is not.

        Without this the announcement alone would be enough, and a peer could claim the
        voice of any proof on the chain by listing its token id.
        """
        result = self._publishers(
            {"peer-a": ["proof-1"]},
            attested=set(),
            token_ids=lambda c: c.token_id,
        )
        self.assertEqual(result, {})

    def test_a_proof_nobody_announced_belongs_to_nobody(self):
        result = self._publishers({}, attested=set(), token_ids=lambda c: c.token_id)
        self.assertEqual(result, {})

    def test_a_proof_two_peers_claim_is_attributed_to_neither(self):
        # Only one of them controls it, and crediting the wrong one hands a peer the
        # voice another peer paid for -- the rule `peer_by_contract_instance` applies to
        # a payment address.
        result = self._publishers(
            {"peer-a": ["proof-1"], "peer-b": ["proof-1"]},
            attested={("peer-a", "proof-1"), ("peer-b", "proof-1")},
            token_ids=lambda c: c.token_id,
        )
        self.assertEqual(result, {})

    def test_a_peer_may_publish_through_several_proofs(self):
        result = self._publishers(
            {"peer-a": ["proof-1", "proof-2"]},
            attested={("peer-a", "proof-1"), ("peer-a", "proof-2")},
            token_ids=lambda c: c.token_id,
        )
        self.assertEqual(result, {"proof-1": "peer-a", "proof-2": "peer-a"})


class _Parsed:
    """Stands in for ``celaut_pb2.Peer()``: parses the stub keyed by the blob it is given."""

    def __init__(self, advertisements):
        self._advertisements = advertisements
        self.reputation_proofs = []

    def ParseFromString(self, blob):
        self.reputation_proofs = self._advertisements[blob].reputation_proofs


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OpinionReadingTests(unittest.TestCase):

    def _read(self, collected, owners, own=()):
        with mock.patch(
            "src.reputation_system.interface._opinion_readers",
            return_value={"ergo": lambda node_id: collected},
        ), mock.patch(
            "src.reputation_system.interface._own_proof_ids", return_value=tuple(own)
        ):
            return onchain_indexer.opinions_for("peer-a", owners)

    def test_an_unattributable_proof_is_dropped_rather_than_stored(self):
        """Where this parts from the donation indexer, on purpose.

        A donation is money that really moved and can be credited retroactively when its
        donor is introduced. An opinion is re-read in full on every refresh, so a
        publisher introduced later is picked up on the next pass and nothing is lost.
        """
        rows = self._read([opinion("stranger-proof")], owners={})
        self.assertEqual(rows, [])

    def test_a_publishers_boxes_are_netted_before_the_row_is_written(self):
        """One proof speaks once, however many boxes it split its stake into."""
        rows = self._read(
            [opinion("proof-1", amount=1, box="b1"), opinion("proof-1", amount=1, box="b2")],
            owners={"proof-1": "pub-1"},
        )
        self.assertEqual(len(rows), 1)
        proof_id, publisher, verdict = rows[0]
        self.assertEqual((proof_id, publisher), ("proof-1", "pub-1"))
        self.assertAlmostEqual(verdict, 0.5)

    def test_polarity_survives_into_the_row(self):
        rows = self._read(
            [opinion("proof-1", positive=False)], owners={"proof-1": "pub-1"}
        )
        self.assertLess(rows[0][2], 0)

    def test_a_proof_that_cancels_itself_out_says_nothing(self):
        rows = self._read(
            [
                opinion("proof-1", positive=True, box="b1"),
                opinion("proof-1", positive=False, box="b2"),
            ],
            owners={"proof-1": "pub-1"},
        )
        self.assertEqual(rows[0][2], 0.0)

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
                onchain_indexer.opinions_for("peer-a", {"proof-1": "pub-1"})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RefreshTests(unittest.TestCase):

    def test_an_unreadable_peer_leaves_its_stored_rows_alone(self):
        """The chain said something last hour; an unreachable explorer is not a retraction.

        Note the asymmetry with the routing path: *there*, a failure zeroes every
        candidate. *Here*, a failure changes nothing at all. Both refuse to let one
        unreadable thing invent a verdict.
        """
        replaced = []

        def opinions_for(peer_id, owners):
            if peer_id == "peer-b":
                raise RuntimeError("explorer unreachable")
            return [("proof-1", "pub-1", 0.25)]

        with mock.patch.object(
            onchain_indexer, "publisher_peers", return_value={"proof-1": "pub-1"}
        ), mock.patch.object(
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

    def test_no_known_publisher_means_no_work_and_no_error(self):
        # What a young node looks like: nobody it knows has proved it publishes anything,
        # so no opinion out there is attributable.
        with mock.patch.object(onchain_indexer, "publisher_peers", return_value={}):
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

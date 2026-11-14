"""Descendant reads across nodes (#631), with more than one node in one process.

Each node has its own database and identity key. A call to a peer goes through the
contract of ``BeeClient.get_balance_statement``: the fake transport changes to the
database and the key of the peer, runs ``balance_statement.answer`` there, and changes
back. ``fetch``, ``statement_check.read``, ``_ask_peer`` and the judges are the real code.

The tree: on A, the client g, its instance p, and p's child c, delegated to B (A's alias
``c_alias``, B's token ``c_onB``). On B, c_onB's child d, delegated to D (B's alias
``d_alias``, D's token ``d_onD``).
"""

import sqlite3
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config

    load_example_config()
    import grpc
    from mnemonic import Mnemonic

    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.identity import node_identity
    from src.manager import balance_statement, statement_check
    from src.utils.cost_functions.general_cost_functions import (
        RATE_STATEMENT_QUERY,
        RATE_STATEMENT_ROW,
    )
    from src.utils.monetary import prices
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


class _PeerError(Exception):
    """A gRPC error of a peer, with its status code."""

    def __init__(self, code, text):
        super().__init__(f"{code.name}: {text}")
        self._code = code

    def code(self):
        return self._code


class _Node:
    def __init__(self, network, name, mnemonic=None):
        self.name = name
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        create_tables(self.connection.cursor())
        self.connection.commit()
        self.mnemonic = mnemonic or Mnemonic("english").generate()
        self.clients = {}
        with network.on(self):
            self.peer_id = node_identity.get_node_public_key_hex()


class _On:
    def __init__(self, network, node):
        self.network, self.node = network, node

    def __enter__(self):
        self.before = self.network.current
        self.network.current = self.node
        SQLConnection._connection = self.node.connection
        return self.node

    def __exit__(self, *exc):
        self.network.current = self.before
        if self.before is not None:
            SQLConnection._connection = self.before.connection


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class _Network(unittest.TestCase):
    """Nodes A, B and D, and the tree of the module docstring."""

    def setUp(self):
        self.sc = SQLConnection()
        self._original_connection = SQLConnection._connection
        self.addCleanup(setattr, SQLConnection, "_connection", self._original_connection)
        self.current = None
        self.nodes = {}
        self.faults = []
        self.evidences = []
        self.lie = {}
        for target, kwargs in (
            ((node_identity, "get_identity_mnemonic"),
             {"side_effect": lambda: self.current.mnemonic}),
            ((statement_check, "penalise_once"), {"side_effect": self._penalise}),
            ((statement_check, "still_pays"), {"return_value": True}),
        ):
            patcher = patch.object(*target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, kwargs in (
            ("src.utils.bee_client.BeeClient.get_balance_statement", {"side_effect": self._call}),
            ("src.identity.grpc_transport.peer_channel",
             {"side_effect": lambda peer_id=None, **_: peer_id}),
            ("src.manager.manager.get_client_id_on_other_peer",
             {"side_effect": lambda peer_id: self.current.clients[peer_id]}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.a, self.b, self.d = self.node("A"), self.node("B"), self.node("D")
        self.link(self.a, self.b, "a_at_B")
        self.link(self.b, self.d, "b_at_D")
        with self.on(self.d):
            self.sc.add_client("b_at_D", 10 ** 7, None)
            self.add_instance("d_onD", "b_at_D", 500)
            self.maintenance("d_onD", 10)
        with self.on(self.b):
            self.sc.add_client("a_at_B", 10 ** 7, None)
            self.add_instance("c_onB", "a_at_B", 5000)
            self.delegated_child("c_onB", "d_alias", "d_onD", self.d, 500)
        with self.on(self.a):
            self.sc.add_client("g", 10 ** 7, None)
            self.add_instance("p", "g", 50000)
            self.delegated_child("p", "c_alias", "c_onB", self.b, 5000)
            # A read of c, which relays the child_bound rows of c, linked d on B (#631).
            self.sc.record_descendant_links(self.b.peer_id, [ledger.instance_ref("d_alias")])

    def node(self, name, mnemonic=None):
        node = _Node(self, name, mnemonic)
        self.nodes[node.peer_id] = node
        return node

    def on(self, node):
        return _On(self, node)

    def link(self, node, peer, client_id):
        """``node`` has the client ``client_id`` on ``peer``."""
        node.clients[peer.peer_id] = client_id
        node.connection.execute("INSERT INTO peer (id, local_client_id) VALUES (?, ?)",
                                (peer.peer_id, client_id))
        node.connection.commit()

    def _call(self, channel, client_id, token="", from_seq=0, to_seq=0, timeout=None,
              instance_ref="", remaining_hops=None):
        peer = self.nodes[channel]
        with self.on(peer):
            lie = self.lie.get(peer.name)
            if lie is not None:
                return lie(client_id, token, from_seq, to_seq, instance_ref)
            try:
                return balance_statement.answer(
                    client_id, token=token, from_seq=from_seq, to_seq=to_seq,
                    allow_debt=False, instance_ref=instance_ref,
                    remaining_hops=remaining_hops)
            except balance_statement.StatementRefused as e:
                raise _PeerError(grpc.StatusCode.PERMISSION_DENIED, str(e))
            except balance_statement.StatementUnavailable as e:
                raise _PeerError(grpc.StatusCode.UNIMPLEMENTED, str(e))
            except ledger.InsufficientBalance as e:
                raise _PeerError(grpc.StatusCode.RESOURCE_EXHAUSTED, str(e))

    def _penalise(self, peer_id, account, reason, problem, seq=0, window=0,
                  per_account=False, amount=None, evidence=None):
        self.faults.append((self.current.name, self.nodes[peer_id].name, reason))
        self.evidences.append(evidence)

    def add_instance(self, token, father, mu):
        self.sc.add_local_instance(
            father_id=father, container_ip="10.0.0.2", container_id=token, name=token,
            balance_mu=mu, serialized_instance="", service_id="ab" * 32, virtualizer="ch",
            disk_space=0, envs="", movements=(ledger.Movement(ledger.START, mu),))

    def delegated_child(self, father, alias, external_token, peer, mu):
        self.sc.add_delegated_instance(
            father_id=father, alias=alias, external_token=external_token,
            peer_id=peer.peer_id, serialized_instance=b"", service_id="ef" * 32,
            balance_mu=mu, peer_balance_mu=mu, movements=(ledger.Movement(ledger.START, mu),),
            sent_peer_mu=0, mark=None)
        seq = self.sc.move_balance(father, -mu, ledger.CHILD_START)
        self.sc.bind_child(father, seq, alias, service_id="ef" * 32)

    def maintenance(self, token, count, mu=5):
        for _ in range(count):
            self.sc.move_balance(token, -mu, ledger.MAINTENANCE,
                                 detail={"seconds": "60", "mem_limit": "1000"})

    def read(self, ref, token="p", from_seq=1):
        """g reads the descendant ``ref`` on A."""
        with self.on(self.a):
            return balance_statement.answer("g", token=token, instance_ref=ref,
                                            from_seq=from_seq, allow_debt=False)

    def charge_problems_of_b(self):
        """What A's check of its own account on B finds (``maintain`` does the same)."""
        with self.on(self.a):
            statement = statement_check.read(self.b.peer_id, "a_at_B", from_seq=1)
            rows = statement_check.rows_of(statement)
            queries = [r for r in rows if r.kind == ledger.STATEMENT_QUERY and r.amount < 0]
            counters = dict(self.sc.get_statement_counters(self.b.peer_id))
            counters["verified"] += len(queries)
            counters["verified_mu"] += -sum(r.amount for r in queries)
            p = prices()
            rates = {RATE_STATEMENT_QUERY: p.statement_query_mu,
                     RATE_STATEMENT_ROW: p.statement_row_mu}
            return statement_check.query_charge_problems(rows, counters, rates)


class RelayedReadChargeTests(_Network):
    """A peer that relays a read charges one full read for each peer that it asks. The
    books of the node that asked count that, so an honest relay is no overcharge."""

    def test_a_relayed_page_read_is_no_overcharge(self):
        statement = self.read(ledger.instance_ref("d_alias"))

        self.assertTrue(statement.relayed)
        self.assertEqual(len(statement.movements), 11)
        self.assertEqual(self.charge_problems_of_b(), [])

    def test_a_read_that_the_peer_asks_more_peers_for_is_no_overcharge(self):
        with self.on(self.b):
            for i in range(3):
                self.delegated_child("c_onB", f"e{i}_alias", f"e{i}_onD", self.d, 10)
        # The peers signed a link to that ref, so each of them is asked.
        with self.on(self.a):
            self.sc.record_descendant_links(self.b.peer_id, ["00" * 32])
        with self.on(self.b):
            self.sc.record_descendant_links(self.d.peer_id, ["00" * 32])
        with self.assertRaises(balance_statement.StatementRefused):
            self.read("00" * 32)

        self.assertEqual(self.charge_problems_of_b(), [])

    def test_a_peer_that_charges_more_than_its_fan_out_is_still_an_overcharge(self):
        with self.on(self.b):
            # One more read than the budget lets B ask for.
            self.sc.move_balance("a_at_B", -prices().statement_query_mu * 16,
                                 ledger.STATEMENT_QUERY, instance_token="c_onB",
                                 detail={"descendant": "x", "from_seq": "0", "to_seq": "0",
                                         "rows": "0", "peers": "16"})
        self.read(ledger.instance_ref("d_alias"), from_seq=0)

        self.assertTrue(self.charge_problems_of_b())


class BoundStatementTests(_Network):
    """The node signs the instance_ref that was asked for with the statement, so a peer
    cannot give the statement of another account for it and say so later."""

    def test_the_statement_names_the_ref_that_the_caller_asked_for(self):
        ref = ledger.instance_ref("d_alias")

        statement = self.read(ref)

        self.assertEqual(statement.instance_ref, ref)
        self.assertTrue(balance_statement.verify(statement, self.a.peer_id))
        changed = type(statement)()
        changed.CopyFrom(statement)
        changed.instance_ref = ledger.instance_ref("c_alias")
        self.assertFalse(balance_statement.verify(changed, self.a.peer_id))

    def test_a_statement_of_another_account_is_refused_and_scored(self):
        with self.on(self.b):
            self.add_instance("x_onB", "a_at_B", 777)
        self.lie["B"] = lambda client_id, token, f, t, ref: balance_statement._answer_account(
            client_id, "x_onB", f, t, False, with_dead=True)

        with self.assertRaises(balance_statement.StatementUnavailable):
            self.read(ledger.instance_ref("c_alias"))

        self.assertIn(("A", "B", "statement_inconsistent"), self.faults)

    def test_a_statement_signed_for_another_ref_is_refused(self):
        def other_ref(client_id, token, f, t, ref):
            return balance_statement._answer_account(
                client_id, token, f, t, False, with_dead=True,
                instance_ref=ledger.instance_ref("x_onB"))
        self.lie["B"] = other_ref

        with self.assertRaises(balance_statement.StatementUnavailable):
            self.read(ledger.instance_ref("c_alias"))

    def test_a_read_without_ref_has_no_ref(self):
        with self.on(self.a):
            statement = balance_statement.answer("g", token="p", from_seq=1)

        self.assertEqual(statement.instance_ref, "")


class KeptHeadTests(_Network):
    """A chain only grows. A peer that signs another chain for a descendant that it showed
    before is scored, and the node keeps the first head."""

    def setUp(self):
        super().setUp()
        self.ref = ledger.instance_ref("c_alias")
        self.ref_on_b = ledger.instance_ref("c_onB")
        with self.on(self.b):
            self.maintenance("c_onB", 3)
        self.first = self.read(self.ref)

    def kept(self):
        with self.on(self.a):
            return self.sc.descendant_head(self.b.peer_id, self.ref_on_b)

    def other_history(self, rows, mu):
        """B signs a new history of c_onB: the same key, another database."""
        # The same name too: for every other node it is B.
        forged = self.node("B", mnemonic=self.b.mnemonic)
        forged.clients = self.b.clients
        self.nodes[self.b.peer_id] = forged
        with self.on(forged):
            self.sc.add_client("a_at_B", 10 ** 7, None)
            self.add_instance("c_onB", "a_at_B", 5000)
            self.maintenance("c_onB", rows, mu=mu)

    def test_a_chain_that_grows_is_read(self):
        with self.on(self.b):
            self.maintenance("c_onB", 2)

        second = self.read(self.ref)

        self.assertEqual(second.head_seq, self.first.head_seq + 2)
        self.assertEqual(self.kept()["head_seq"], second.head_seq)
        self.assertEqual(self.faults, [])

    def test_a_head_that_goes_back_is_scored(self):
        self.other_history(rows=1, mu=1)

        with self.assertRaises(balance_statement.StatementUnavailable):
            self.read(self.ref)

        self.assertIn(("A", "B", "statement_inconsistent"), self.faults)
        self.assertEqual(self.kept()["head_hash"], self.first.head_hash)

    def test_the_same_seqs_with_other_rows_are_scored(self):
        self.other_history(rows=5, mu=1)

        with self.assertRaises(balance_statement.StatementUnavailable):
            self.read(self.ref)

        self.assertIn(("A", "B", "statement_inconsistent"), self.faults)
        # The two pages that B signed are the evidence: any node proves the fault from
        # them with the key of B alone (#652).
        from src.reputation_system import fault_proofs
        evidence = self.evidences[self.faults.index(("A", "B", "statement_inconsistent"))]
        self.assertIsNotNone(evidence)
        self.assertEqual(len(evidence.statements), 2)
        proven = fault_proofs.check(evidence)
        self.assertEqual(proven.accused, self.b.peer_id)

    def test_a_longer_chain_that_does_not_follow_the_head_is_scored(self):
        self.other_history(rows=6, mu=1)

        with self.assertRaises(balance_statement.StatementUnavailable):
            self.read(self.ref)

        self.assertIn(("A", "B", "statement_inconsistent"), self.faults)

    def test_the_head_read_after_a_tunnel_keeps_the_first_head(self):
        from src.tunneling import rpc_tunnel

        self.other_history(rows=1, mu=1)
        with self.on(self.a):
            row = self.sc.get_delegated_instance_by_alias("c_alias")
            with patch.object(rpc_tunnel, "HEAD_REFRESH_S", 0):
                rpc_tunnel._refresh_head(row, self.ref_on_b, 5, None)

        self.assertIn(("A", "B", "statement_inconsistent"), self.faults)
        self.assertEqual(self.kept()["head_hash"], self.first.head_hash)

    def test_an_account_with_no_rows_left_is_no_conflict(self):
        empty = type(self.first)(head_seq=0, head_hash="")
        with self.on(self.a):
            self.assertIsNone(balance_statement.head_conflict(
                self.b.peer_id, self.ref_on_b, empty))


if __name__ == "__main__":
    unittest.main()

"""Tests for the tunnel of an ancestor to a descendant (#631, ``rpc_tunnel``).

The database is a real in-memory one with the real schema, so the walk through the
``child_bound`` rows runs for real. The instance record and its internal IP are
mocked, as in ``test_service_tunnel.py``, and the service is a real TCP server on
loopback. The hop to a peer replaces only the gRPC call: the relay, its meter and its
records run for real.
"""

import json
import socket
import sqlite3
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    import grpc
    from mnemonic import Mnemonic
    from protos import celaut_pb2 as celaut
    from src.database import ledger
    from src.database.migrate import create_tables
    from src.database.sql_connection import SQLConnection
    from src.identity import execution_receipt, node_identity
    from src.tunneling import rpc_tunnel
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

DEADLINE_S = 5.0


def _deadline():
    expires_at = time.monotonic() + DEADLINE_S
    return lambda: time.monotonic() < expires_at


def _echo_server() -> int:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(DEADLINE_S)
    port = server.getsockname()[1]

    def run():
        try:
            conn, _ = server.accept()
            with conn:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        return
                    conn.sendall(data)
        except OSError:
            pass
        finally:
            server.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def _instance(port: int) -> bytes:
    return celaut.Instance(
        api=celaut.Service.Api(slot=[celaut.Service.Api.Slot(
            port=port, transport=celaut.Service.Api.Protocol(tags=["tcp"]))]),
        uri_slot=[celaut.Instance.Uri_Slot(internal_port=port)],
    ).SerializeToString()


class _RpcError(Exception):
    def __init__(self, code, details="peer-1 at 10.9.9.9 failed"):
        super().__init__(details)
        self._code = code

    def code(self):
        return self._code


class _Tree(unittest.TestCase):
    """The tree: client g, its instance p, the child c of p, and the child d of c.
    It has no tests of its own."""

    def setUp(self):
        self.sc = SQLConnection()
        self._original = SQLConnection._connection
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        create_tables(connection.cursor())
        connection.commit()
        SQLConnection._connection = connection
        self.addCleanup(self._restore)

        mnemonic = Mnemonic("english").generate()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(node_identity, "get_identity_mnemonic",
                                         return_value=mnemonic))
        self.peer_id = node_identity.get_node_public_key_hex()
        self.port = _echo_server()
        # The instances that run on "this node". A test of a chain of nodes adds the
        # instances of the next nodes: all the nodes share one database here.
        self.local = {"c", "d"}
        stack.enter_context(patch.object(
            rpc_tunnel.sc, "get_internal_instance",
            side_effect=lambda id: _instance(self.port) if id in self.local else None))
        stack.enter_context(patch.object(
            rpc_tunnel.sc, "get_internal_ip",
            side_effect=lambda id: "127.0.0.1" if id in self.local else None))
        self.spend = stack.enter_context(patch("src.manager.manager.spend_mu",
                                               return_value=True))
        # Open costs 1 MU, and the traffic is free: the open charge is the one that the
        # tests look at.
        self.prices = stack.enter_context(patch("src.tunneling.rpc_tunnel.prices"))
        self.prices.return_value.tunnel_open_mu = 1
        self.prices.return_value.net_mu_per_gib = 0
        # The key that signs the receipts and names the executions.
        self.receipt_peer = execution_receipt.node_peer_id()

        self.sc.add_client("g", 1000, None)
        self.add_instance("p", "g", 500)
        self.child("p", "c", 200)
        self.child("c", "d", 50)

    def _restore(self):
        # A head read runs on its own thread after a hop. It must end before the
        # database closes.
        for thread in threading.enumerate():
            if thread.name == "descendant-head":
                thread.join(DEADLINE_S)
        SQLConnection._connection.close()
        SQLConnection._connection = self._original

    def add_instance(self, token, father, mu):
        self.sc.add_local_instance(
            father_id=father, container_ip="10.0.0.2", container_id=token, name=token,
            balance_mu=mu, serialized_instance="", service_id="s", virtualizer="ch",
            disk_space=0, envs="", movements=(ledger.Movement(ledger.START, mu),),
        )

    def child(self, father, token, mu):
        seq = self.sc.move_balance(father, -mu, ledger.CHILD_START)
        self.add_instance(token, father, mu)
        self.sc.bind_child(father, seq, token, service_id="ab")

    def delegate(self, father="p", alias="alias", peer_token="peer-token", peer="peer-1"):
        self.sc.add_delegated_instance(
            father_id=father, alias=alias, external_token=peer_token, peer_id=peer,
            serialized_instance=b"", service_id="s", balance_mu=10, peer_balance_mu=1,
            movements=(ledger.Movement(ledger.START, 10),), sent_peer_mu=0, mark=None,
        )
        seq = self.sc.move_balance(father, -10, ledger.CHILD_START)
        self.sc.bind_child(father, seq, alias)

    def open(self, token="p", ref="d", payload=(b"ping",), receipts=False, hops=None):
        first = celaut.DescendantTunnel(token=token, instance_ref=ledger.instance_ref(ref),
                                        slot=str(self.port), receipts=receipts)
        if hops is not None:
            first.remaining_hops = hops
        return rpc_tunnel.service_tunnel(iter([first, *payload]), is_active=_deadline())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DescendantTunnelTests(_Tree):

    # -- local descendants --------------------------------------------------------

    def test_an_ancestor_reaches_a_local_grandchild_and_the_grandchild_pays(self):
        conn, relay = self.open()
        replies = list(relay)
        conn.close()

        self.assertEqual(b"".join(r for r in replies if isinstance(r, bytes)), b"ping")
        payers = {call.kwargs["id"] for call in self.spend.call_args_list}
        self.assertEqual(payers, {"d"})

    def test_the_ledger_rows_name_the_execution_by_the_instance_ref(self):
        conn, relay = self.open()
        list(relay)
        conn.close()
        cause = self.spend.call_args_list[0].kwargs["detail"]
        if self.receipt_peer is None:
            self.skipTest("This node has no identity key.")
        self.assertEqual(cause["execution_name"], execution_receipt.execution_name_by_ref(
            ledger.instance_ref("d"), self.receipt_peer))

    def test_the_receipts_name_the_execution_by_the_instance_ref(self):
        if self.receipt_peer is None:
            self.skipTest("This node has no identity key.")
        conn, relay = self.open(receipts=True)
        replies = list(relay)
        conn.close()
        receipts = [r for r in replies if isinstance(r, celaut.ExecutionReceipt)]

        self.assertTrue(receipts)
        ref = ledger.instance_ref("d")
        for receipt in receipts:
            self.assertEqual(execution_receipt.problems(
                receipt, peer_id=self.receipt_peer, instance_ref=ref), [])
            self.assertNotEqual(receipt.execution_name,
                                execution_receipt.execution_name("d", self.receipt_peer))

    def test_the_close_receipt_is_anchored_in_the_chain_of_the_descendant_and_agrees(self):
        from collections import namedtuple
        from src.identity import receipt_statement

        # The charge of the open writes its row here, as ``spend_mu`` does for real.
        self.spend.side_effect = lambda id, amount_mu, debug_mode, kind, detail: (
            self.sc.move_balance(id, -amount_mu, kind, detail=detail) or True)
        conn, relay = self.open(receipts=True)
        replies = list(relay)
        conn.close()
        receipts = [r for r in replies if isinstance(r, celaut.ExecutionReceipt)]
        close = next(r for r in receipts if r.kind == execution_receipt.KIND_CLOSE)

        Row = namedtuple("Row", "seq ts amount kind detail")
        rows = [Row(int(r["seq"]), int(r["ts"]), int(r["amount_mu"]), r["kind"],
                    json.loads(r["detail"] or "{}"))
                for r in SQLConnection._connection.execute(
                    "SELECT * FROM balance_movements WHERE account_id = 'd' ORDER BY seq")]
        anchors = [r for r in rows if r.detail.get("close")]

        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0].amount, 0)
        self.assertEqual(anchors[0].detail["receipt_digest"],
                         execution_receipt.receipt_digest(close))
        report = receipt_statement.check(receipts, rows, peer_id=self.receipt_peer)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["checked"], 1)

    def test_a_ref_that_no_chain_links_is_refused(self):
        self.add_instance("loose", "g", 10)
        for ref in ("loose", "nobody"):
            with self.subTest(ref=ref), self.assertRaises(rpc_tunnel.TunnelRefused):
                self.open(ref=ref)

    def test_a_token_that_this_node_does_not_know_is_refused(self):
        with self.assertRaises(rpc_tunnel.TunnelRefused) as raised:
            self.open(token="ghost")
        self.assertNotIn("ghost", str(raised.exception))

    def test_a_stopped_descendant_is_unavailable(self):
        self.sc.settle_stop("d", "manual_kill", father_id="c")
        with self.assertRaises(rpc_tunnel.TunnelUnavailable):
            self.open()

    def test_a_stopped_parent_still_opens_the_tunnel_to_its_child(self):
        # That is the case of the issue: the parent is dead, the ancestor is the witness.
        self.sc.settle_stop("c", "manual_kill", father_id="p")
        conn, relay = self.open()
        self.assertEqual(b"".join(r for r in relay if isinstance(r, bytes)), b"ping")
        conn.close()

    def test_a_stopped_grandparent_still_opens_the_tunnel_to_its_grandchild(self):
        # The holder of the token of p keeps the right to reach the tree below p, also
        # when p and c both stopped. The grandchild pays the traffic.
        self.sc.settle_stop("c", "manual_kill", father_id="p")
        self.sc.settle_stop("p", "manual_kill", father_id="g")
        conn, relay = self.open()
        self.assertEqual(b"".join(r for r in relay if isinstance(r, bytes)), b"ping")
        conn.close()
        self.assertEqual({call.kwargs["id"] for call in self.spend.call_args_list}, {"d"})

    def test_a_handshake_without_instance_ref_is_refused(self):
        first = celaut.DescendantTunnel(token="p", slot="1")
        with self.assertRaises(rpc_tunnel.TunnelError) as raised:
            rpc_tunnel.service_tunnel(iter([first]))
        self.assertIn("instance_ref", str(raised.exception))

    def test_no_refusal_names_the_token_of_the_descendant(self):
        with self.assertRaises(rpc_tunnel.TunnelError) as raised:
            rpc_tunnel.service_tunnel(iter([celaut.TokenMessage(token="d", slot="22")]))
        self.assertNotIn("'d'", str(raised.exception))
        self.assertIn("not declared", str(raised.exception))

    # -- a hop to a peer ----------------------------------------------------------

    def hop(self, responses, ref="alias", hops=None, payload=(b"ping",), receipts=False):
        """Open a tunnel through the peer. ``responses`` is what the peer streams back,
        or an exception that the call raises before it streams."""
        sent = []
        channel = MagicMock()

        def service_tunnel(chan, outbound):
            self.assertIs(chan, channel)
            sent.extend(outbound)

            def stream():
                if isinstance(responses, Exception):
                    raise responses
                yield from responses
            return stream()

        with patch.object(rpc_tunnel, "_peer_channel", return_value=channel) as dial, \
                patch("src.utils.bee_client.BeeClient.service_tunnel",
                      side_effect=service_tunnel):
            conn, relay = self.open(ref=ref, hops=hops, payload=payload,
                                    receipts=receipts)
            try:
                replies = list(relay)
                error = None
            except rpc_tunnel.TunnelError as e:
                replies, error = [], e
        self.dial = dial
        return sent, replies, error, channel

    def test_a_delegated_descendant_is_reached_through_its_peer_with_the_token_of_this_node(self):
        self.delegate()
        sent, replies, error, channel = self.hop([b"pong"], hops=4)

        self.assertIsNone(error)
        self.assertEqual(replies, [b"pong"])
        self.dial.assert_called_once_with("peer-1")
        first = sent[0]
        self.assertIsInstance(first, celaut.DescendantTunnel)
        self.assertEqual(first.token, "peer-token")
        self.assertEqual(first.instance_ref, ledger.instance_ref("peer-token"))
        self.assertEqual(first.remaining_hops, 3)
        # The node asks the next node for receipts: it keeps the last valid one.
        self.assertTrue(first.receipts)
        self.assertEqual(sent[1:], [b"ping"])
        channel.close.assert_called()

    def test_the_leg_of_this_node_is_charged_to_the_alias_of_the_descendant(self):
        self.delegate()
        self.prices.return_value.tunnel_open_mu = 2
        self.hop([b"pong"])
        rows = [dict(r) for r in SQLConnection._connection.execute(
            "SELECT kind, amount_mu, detail FROM balance_movements WHERE account_id = 'alias'")]
        tunnel = [r for r in rows if r["kind"] == ledger.TUNNEL]
        self.assertEqual(int(tunnel[0]["amount_mu"]), -2)
        self.assertNotIn("peer-token", json.dumps(rows))

    def test_a_descendant_below_a_delegated_instance_is_asked_with_the_same_ref(self):
        self.delegate()
        sent, replies, error, _ = self.hop([b"pong"], ref="far")
        self.assertIsNone(error)
        self.assertEqual(sent[0].instance_ref, ledger.instance_ref("far"))
        self.assertEqual(sent[0].token, "peer-token")

    def test_a_hop_that_works_is_written_down(self):
        self.delegate()
        ref = ledger.instance_ref("peer-token")
        self.sc.record_descendant_tunnel("peer-1", ref, ok=False, ts=int(time.time()) - 10)
        self.hop([b"pong"])
        self.assertIsNone(self.sc.descendant_tunnel_failure("peer-1", ref))

    def test_a_peer_that_does_not_answer_is_written_down_and_named_to_nobody(self):
        self.delegate()
        sent, replies, error, _ = self.hop(_RpcError(grpc.StatusCode.UNAVAILABLE))

        self.assertIsInstance(error, rpc_tunnel.TunnelUnavailable)
        self.assertNotIn("peer-1", str(error))
        self.assertNotIn("10.9.9.9", str(error))
        self.assertIsNotNone(self.sc.descendant_tunnel_failure(
            "peer-1", ledger.instance_ref("peer-token")))

    def test_a_refusal_of_the_peer_is_a_refusal_and_not_a_failure(self):
        self.delegate()
        sent, replies, error, _ = self.hop(_RpcError(grpc.StatusCode.PERMISSION_DENIED))

        self.assertIsInstance(error, rpc_tunnel.TunnelRefused)
        self.assertIsNone(self.sc.descendant_tunnel_failure(
            "peer-1", ledger.instance_ref("peer-token")))

    def test_a_peer_that_cannot_be_reached_is_unavailable(self):
        self.delegate()
        with patch.object(rpc_tunnel, "_peer_channel",
                          side_effect=ConnectionError("No address for peer-1")):
            with self.assertRaises(rpc_tunnel.TunnelUnavailable) as raised:
                self.open(ref="alias")
        self.assertNotIn("peer-1", str(raised.exception))
        self.assertIsNotNone(self.sc.descendant_tunnel_failure(
            "peer-1", ledger.instance_ref("peer-token")))

    def test_the_receipts_of_the_peer_are_not_passed_on(self):
        self.delegate()
        foreign = celaut.ExecutionReceipt(kind="checkpoint", peer_id="ff" * 32)
        sent, replies, error, _ = self.hop([b"pong", foreign])
        self.assertEqual(replies, [b"pong"])

    def test_the_hop_budget_ends_the_tunnel_before_the_peer(self):
        self.delegate()
        with patch.object(rpc_tunnel, "_peer_channel") as dial:
            for hops in (0, 1):
                with self.subTest(hops=hops), \
                        self.assertRaises(rpc_tunnel.TunnelExhausted):
                    self.open(ref="alias", hops=hops)
        dial.assert_not_called()

    def test_a_used_up_budget_below_is_not_written_as_a_failure(self):
        # The caller sets the budget. A small budget is not a fault of the peer.
        self.delegate()
        sent, replies, error, _ = self.hop(_RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED))

        self.assertIsInstance(error, rpc_tunnel.TunnelExhausted)
        self.assertNotIn("peer-1", str(error))
        self.assertIsNone(self.sc.descendant_tunnel_failure(
            "peer-1", ledger.instance_ref("peer-token")))

    def test_the_relay_signs_its_own_leg_by_the_instance_ref_of_the_caller(self):
        if self.receipt_peer is None:
            self.skipTest("This node has no identity key.")
        self.delegate()
        sent, replies, error, _ = self.hop([b"pong"], receipts=True)

        self.assertIsNone(error)
        self.assertTrue(sent[0].receipts)
        self.assertEqual([r for r in replies if isinstance(r, bytes)], [b"pong"])
        receipts = [r for r in replies if isinstance(r, celaut.ExecutionReceipt)]
        self.assertTrue(receipts)
        self.assertEqual(receipts[-1].kind, "close")
        ref = ledger.instance_ref("alias")
        for receipt in receipts:
            self.assertEqual(receipt.peer_id, self.receipt_peer)
            self.assertEqual(execution_receipt.problems(
                receipt, peer_id=self.receipt_peer, instance_ref=ref), [])
            self.assertNotIn("peer-token", str(receipt))

    def test_a_stopped_delegated_descendant_is_unavailable(self):
        self.delegate()
        self.sc.settle_stop("alias", "peer_stopped", delegated=True, father_id="p")
        with patch.object(rpc_tunnel, "_peer_channel") as dial:
            with self.assertRaises(rpc_tunnel.TunnelUnavailable):
                self.open(ref="alias")
        dial.assert_not_called()

    def _many(self):
        self.delegate(alias="a1", peer_token="t1", peer="peer-1")
        self.delegate(alias="a2", peer_token="t2", peer="peer-2")
        return ledger.instance_ref("far")

    def test_with_many_delegated_children_no_head_means_read_first(self):
        self._many()
        with patch("src.manager.statement_check.read") as probe:
            with self.assertRaises(rpc_tunnel.TunnelRefused):
                self.open(ref="far", hops=5)
        # The node asks no peer: the caller of a tunnel has paid nothing yet.
        probe.assert_not_called()

    def test_with_many_delegated_children_the_one_peer_with_a_head_gets_the_tunnel(self):
        ref = self._many()
        self.sc.record_descendant_head("peer-2", ref, 3, "h")
        with patch("src.manager.statement_check.read") as probe:
            sent, replies, error, _ = self.hop([b"pong"], ref="far", hops=5)

        self.assertIsNone(error)
        probe.assert_not_called()
        self.dial.assert_called_once_with("peer-2")
        self.assertEqual(sent[0].token, "t2")
        self.assertEqual(sent[0].remaining_hops, 2)

    def test_with_many_delegated_children_two_heads_give_no_tunnel(self):
        ref = self._many()
        self.sc.record_descendant_head("peer-1", ref, 3, "h")
        self.sc.record_descendant_head("peer-2", ref, 3, "h")
        with self.assertRaises(rpc_tunnel.TunnelUnavailable):
            self.open(ref="far", hops=5)

    def test_with_two_heads_the_peer_that_linked_the_ref_first_gets_the_tunnel(self):
        ref = self._many()
        self.sc.record_descendant_head("peer-1", ref, 3, "h")
        self.sc.record_descendant_head("peer-2", ref, 3, "h")
        self.sc.record_descendant_links("peer-1", [ref], ts=100)
        self.sc.record_descendant_links("peer-2", [ref], ts=200)
        sent, replies, error, _ = self.hop([b"pong"], ref="far", hops=5)

        self.assertIsNone(error)
        self.dial.assert_called_once_with("peer-1")
        self.assertEqual(sent[0].token, "t1")


_STATUS_OF = {}


def _as_rpc_error(error):
    """The ``TunnelError`` of a node as its caller gets it through gRPC: a status, and
    a text that the node above does not read."""
    if not _STATUS_OF:
        _STATUS_OF.update({
            rpc_tunnel.TunnelRefused: grpc.StatusCode.PERMISSION_DENIED,
            rpc_tunnel.TunnelExhausted: grpc.StatusCode.RESOURCE_EXHAUSTED,
            rpc_tunnel.TunnelUnavailable: grpc.StatusCode.UNAVAILABLE,
        })
    return _RpcError(_STATUS_OF.get(type(error), grpc.StatusCode.INVALID_ARGUMENT),
                     str(error))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RelayChainTests(_Tree):
    """Three nodes, hop by hop: A (this node) delegated ``alias`` to B as ``peer-token``.
    B delegated ``b-alias`` to C as ``t2``. The caller on A holds ``p`` and the
    ``instance_ref`` that B gave to the instance on C (of ``b-alias``).

    The fake gRPC call runs the real ``service_tunnel`` of the next node. All the nodes
    share one database and one key here, but each node uses only the rows of its own
    instances.
    """

    def setUp(self):
        super().setUp()
        self.delegate(father="p", alias="alias", peer_token="peer-token", peer="peer-1")
        self.sc.add_client("b-client", 1000, None)
        self.add_instance("peer-token", "b-client", 100)
        self.local.add("peer-token")
        self.delegate(father="peer-token", alias="b-alias", peer_token="t2", peer="peer-2")
        self.sc.add_client("c-client", 1000, None)
        self.add_instance("t2", "c-client", 100)
        self.local.add("t2")

    def chain(self, hops, payload=(b"ping",), down=()):
        """Open the tunnel on A. ``down``: the peers that do not answer. Returns
        ``(firsts, replies, error)``; ``firsts`` maps each peer to the first message
        that it got."""
        firsts = {}
        channels = {}

        def dial(peer_id):
            if peer_id in down:
                raise ConnectionError(f"No address for {peer_id}")
            channels[peer_id] = MagicMock(name=peer_id)
            channels[peer_id].peer_id = peer_id
            return channels[peer_id]

        def next_node(channel, outbound):
            outbound = iter(outbound)
            first = next(outbound)
            firsts[channel.peer_id] = first

            def stream():
                try:
                    conn, relay = rpc_tunnel.service_tunnel(
                        _prepend(first, outbound), is_active=_deadline())
                except rpc_tunnel.TunnelError as e:
                    raise _as_rpc_error(e)
                try:
                    try:
                        yield from relay
                    except rpc_tunnel.TunnelError as e:
                        raise _as_rpc_error(e)
                finally:
                    conn.close()
            return stream()

        with patch.object(rpc_tunnel, "_peer_channel", side_effect=dial), \
                patch("src.utils.bee_client.BeeClient.service_tunnel",
                      side_effect=next_node):
            try:
                conn, relay = self.open(ref="b-alias", hops=hops, payload=payload)
            except rpc_tunnel.TunnelError as e:
                return firsts, [], e
            try:
                replies, error = list(relay), None
            except rpc_tunnel.TunnelError as e:
                replies, error = [], e
            finally:
                conn.close()
        return firsts, replies, error

    def tunnel_rows(self, account):
        return [dict(r) for r in SQLConnection._connection.execute(
            "SELECT amount_mu, detail FROM balance_movements "
            "WHERE account_id = ? AND kind = ?", (account, ledger.TUNNEL))]

    def test_the_bytes_go_through_each_node_and_back(self):
        firsts, replies, error = self.chain(hops=5)

        self.assertIsNone(error)
        self.assertEqual(b"".join(r for r in replies if isinstance(r, bytes)), b"ping")
        # Each node sends the token that it holds, and one hop less than it got.
        self.assertEqual(firsts["peer-1"].token, "peer-token")
        self.assertEqual(firsts["peer-1"].instance_ref, ledger.instance_ref("b-alias"))
        self.assertEqual(firsts["peer-1"].remaining_hops, 4)
        self.assertEqual(firsts["peer-2"].token, "t2")
        self.assertEqual(firsts["peer-2"].instance_ref, ledger.instance_ref("t2"))
        self.assertEqual(firsts["peer-2"].remaining_hops, 3)

    def test_each_node_meters_its_own_leg(self):
        self.chain(hops=5)

        # A has no account of the instance on C: only the ceilings apply to its leg.
        self.assertEqual(self.tunnel_rows("alias"), [])
        # B charges its leg to the alias that it holds, and C to the instance.
        # The second row moves nothing: it anchors the close receipt of the leg.
        self.assertEqual([int(r["amount_mu"]) for r in self.tunnel_rows("b-alias")], [-1, 0])
        self.assertEqual({call.kwargs["id"] for call in self.spend.call_args_list}, {"t2"})
        # The rows of B name the execution by the instance_ref that B got.
        detail = json.loads(self.tunnel_rows("b-alias")[0]["detail"])
        if self.receipt_peer is not None:
            self.assertEqual(detail["execution_name"], execution_receipt.execution_name_by_ref(
                ledger.instance_ref("b-alias"), self.receipt_peer))

    def test_a_budget_too_small_for_the_chain_stops_below_and_is_no_failure(self):
        firsts, replies, error = self.chain(hops=2)

        self.assertIsInstance(error, rpc_tunnel.TunnelExhausted)
        self.assertEqual(firsts["peer-1"].remaining_hops, 1)
        self.assertNotIn("peer-2", firsts)
        self.assertIsNone(self.sc.descendant_tunnel_failure(
            "peer-1", ledger.instance_ref("b-alias")))

    def test_a_failure_two_nodes_below_names_no_node_and_no_token(self):
        firsts, replies, error = self.chain(hops=5, down=("peer-2",))

        self.assertIsInstance(error, rpc_tunnel.TunnelUnavailable)
        for secret in ("peer-1", "peer-2", "peer-token", "t2", "127.0.0.1"):
            self.assertNotIn(secret, str(error))
        # Each node writes the failure of its own hop, under the ref that it sent.
        self.assertIsNotNone(self.sc.descendant_tunnel_failure(
            "peer-2", ledger.instance_ref("t2")))
        self.assertIsNotNone(self.sc.descendant_tunnel_failure(
            "peer-1", ledger.instance_ref("b-alias")))


def _prepend(first, rest):
    yield first
    yield from rest


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TunnelStatusTests(unittest.TestCase):
    def test_each_class_of_refusal_has_its_status(self):
        try:
            from src.gateway.gateway import _tunnel_status
        except ImportError as e:  # pragma: no cover - environment-dependent
            self.skipTest(f"The gateway does not import here: {e}")
        self.assertEqual(_tunnel_status(rpc_tunnel.TunnelRefused("x")),
                         grpc.StatusCode.PERMISSION_DENIED)
        self.assertEqual(_tunnel_status(rpc_tunnel.TunnelExhausted("x")),
                         grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertEqual(_tunnel_status(rpc_tunnel.TunnelUnavailable("x")),
                         grpc.StatusCode.UNAVAILABLE)
        self.assertEqual(_tunnel_status(rpc_tunnel.TunnelError("x")),
                         grpc.StatusCode.INVALID_ARGUMENT)


if __name__ == "__main__":
    unittest.main()

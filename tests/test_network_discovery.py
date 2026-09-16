"""Asking other nodes to resolve a communication domain (issue #78).

`Gateway.ResolveNetwork` is generic -- any `Service.Network`, not a `pow:`-shaped one
-- because a domain is declared the same way whatever resolves it, and a caller that
had to know in advance which kind it was holding would be doing the resolving itself.

What is pinned here is the part that is a decision rather than plumbing:

* an answer is a list of addresses to *try*. Nothing is opened on the strength of it,
  so the client takes bare addresses and throws the sender's grouping away;
* a reply is untrusted input that becomes outbound requests, so its length is this
  node's decision;
* a peer that is down is one fewer source, never a failed launch;
* and a node **never relays** the question. Two nodes that know each other are a
  cycle of length two, so relaying would turn one request into a flood over a graph
  nobody can see the shape of.

The module is loaded from its file with its database, transport and bee-rpc seams
stubbed -- importing it by name pulls in the whole gRPC stack, which these tests never
exercise. Same device as `test_networks_ancestors.py`.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from protos import celaut_pb2_grpc
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    celaut_pb2_grpc = None  # type: ignore[assignment]


def _load_discovery_module():
    """``src/manager/network_discovery.py`` with its outbound seams replaced."""
    stubbed = ("bee_rpc", "bee_rpc.client", "src.database.sql_connection",
               "src.identity.grpc_transport")
    saved = {name: sys.modules.get(name) for name in stubbed}

    bee_pkg = types.ModuleType("bee_rpc")
    bee_client = types.ModuleType("bee_rpc.client")
    bee_client.client_grpc = lambda **kwargs: iter(())
    bee_pkg.client = bee_client
    sql_stub = types.ModuleType("src.database.sql_connection")
    sql_stub.SQLConnection = type("SQLConnection", (), {"get_peers_id": lambda self: []})
    transport_stub = types.ModuleType("src.identity.grpc_transport")
    transport_stub.peer_channel = lambda peer_id: None

    sys.modules["bee_rpc"] = bee_pkg
    sys.modules["bee_rpc.client"] = bee_client
    sys.modules["src.database.sql_connection"] = sql_stub
    sys.modules["src.identity.grpc_transport"] = transport_stub
    try:
        path = Path(__file__).resolve().parents[1] / "src" / "manager" / "network_discovery.py"
        spec = importlib.util.spec_from_file_location("network_discovery_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # pragma: no cover - environment-dependent
        return None
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _instance(*addresses):
    """One peer instance, at however many addresses."""
    return celaut.Instance(
        uri_slot=[celaut.Instance.Uri_Slot(
            internal_port=1,
            uri=[celaut.Instance.Uri(ip=ip, port=port) for ip, port in addresses],
        )]
    )


def _resolution(*instances):
    return celaut.ConfigurationFile.NetworkResolution(
        tags=["pow:ergo"], peer_instances=list(instances)
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RpcIsWiredUpTests(unittest.TestCase):
    """The generated stub is edited by hand in this repo, so the wiring is asserted.

    `protos/celaut_pb2_grpc.py` already carries a hand-applied import fix, and the
    toolchain that regenerates it (grpcio-tools 1.56, for the pinned protobuf 4.x)
    does not build on every machine this is developed on. A method missing from one
    of the four places it has to appear in fails at call time, in a handshake.
    """

    def test_the_servicer_declares_the_method(self):
        self.assertTrue(hasattr(celaut_pb2_grpc.GatewayServicer, "ResolveNetwork"))

    def test_the_stub_exposes_the_method_on_its_own_path(self):
        channel = MagicMock()
        celaut_pb2_grpc.GatewayStub(channel)

        paths = [call.args[0] for call in channel.stream_stream.call_args_list]
        self.assertIn("/celaut.Gateway/ResolveNetwork", paths)

    def test_the_server_registers_a_handler_for_it(self):
        server = MagicMock()
        celaut_pb2_grpc.add_GatewayServicer_to_server(MagicMock(), server)

        handlers = server.add_generic_rpc_handlers.call_args.args[0][0]
        self.assertIn("/celaut.Gateway/ResolveNetwork", handlers._method_handlers)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AskPeerTests(unittest.TestCase):
    def setUp(self):
        module = _load_discovery_module()
        if module is None:  # pragma: no cover - environment-dependent
            self.skipTest("src/manager/network_discovery.py could not be loaded")
        self.nd = module
        self.network = celaut.Service.Network(tags=["pow:ergo"], formal=b"chain=ergo")

    def _ask(self, answer):
        with patch.object(self.nd.bee, "client_grpc", return_value=iter([answer] if answer else [])), \
             patch.object(self.nd, "celaut_pb2_grpc", MagicMock()), \
             patch.object(self.nd, "peer_channel", return_value=None):
            return self.nd.ask_peer("peer-1", self.network)

    def test_every_address_of_every_instance_is_taken_and_flattened(self):
        """Which addresses belong to the same remote node is the answering peer's
        belief about a third party, and each one is verified on its own anyway."""
        answer = _resolution(
            _instance(("203.0.113.10", 9053), ("203.0.113.11", 9053)),
            _instance(("203.0.113.12", 9053)),
        )

        self.assertEqual(
            self._ask(answer),
            [("203.0.113.10", 9053), ("203.0.113.11", 9053), ("203.0.113.12", 9053)],
        )

    def test_an_answer_is_capped_at_what_this_node_is_willing_to_try(self):
        many = _instance(*[(f"203.0.113.{n}", 9053) for n in range(1, 60)])

        self.assertEqual(len(self._ask(_resolution(many))), self.nd.MAX_ADDRESSES_PER_PEER)

    def test_an_address_with_no_ip_or_no_port_is_dropped(self):
        answer = _resolution(_instance(("", 9053), ("203.0.113.10", 0), ("203.0.113.11", 9053)))

        self.assertEqual(self._ask(answer), [("203.0.113.11", 9053)])

    def test_a_peer_that_answers_nothing_is_not_an_error(self):
        self.assertEqual(self._ask(None), [])

    def test_peer_requests_have_a_finite_deadline(self):
        with patch.object(self.nd.bee, "client_grpc", return_value=iter(())) as rpc, \
             patch.object(self.nd, "celaut_pb2_grpc", MagicMock()), \
             patch.object(self.nd, "peer_channel", return_value=None):
            self.assertEqual(self.nd.ask_peer("peer-1", self.network), [])
        self.assertEqual(rpc.call_args.kwargs["timeout"], 10)

    def test_a_peer_that_fails_is_one_fewer_source_not_a_failed_launch(self):
        def _raise(**kwargs):
            raise RuntimeError("unreachable")

        with patch.object(self.nd.bee, "client_grpc", side_effect=_raise), \
             patch.object(self.nd, "celaut_pb2_grpc", MagicMock()), \
             patch.object(self.nd, "peer_channel", return_value=None):
            self.assertEqual(self.nd.ask_peer("peer-1", self.network), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AskPeersTests(unittest.TestCase):
    def setUp(self):
        module = _load_discovery_module()
        if module is None:  # pragma: no cover - environment-dependent
            self.skipTest("src/manager/network_discovery.py could not be loaded")
        self.nd = module
        self.network = celaut.Service.Network(tags=["pow:ergo"])

    def _ask_peers(self, answers, peer_ids=("p1", "p2", "p3"), limit=None):
        with patch.object(self.nd.sc, "get_peers_id", return_value=list(peer_ids)), \
             patch.object(self.nd, "ask_peer", side_effect=lambda pid, net: answers.get(pid, [])):
            return self.nd.ask_peers(self.network, limit=limit)

    def test_answers_are_pooled_in_ask_order(self):
        found = self._ask_peers({
            "p1": [("203.0.113.10", 9053)],
            "p2": [("203.0.113.11", 9053)],
        })

        self.assertEqual(found, [("203.0.113.10", 9053), ("203.0.113.11", 9053)])

    def test_two_peers_naming_the_same_address_have_confirmed_nothing(self):
        """It is asked once, and agreement buys it no better place: they may well
        have read it off the same list, and treating that as evidence would be a
        trust decision this has no business making."""
        found = self._ask_peers({
            "p1": [("203.0.113.10", 9053), ("203.0.113.11", 9053)],
            "p2": [("203.0.113.11", 9053)],
            "p3": [("203.0.113.11", 9053)],
        })

        self.assertEqual(found, [("203.0.113.10", 9053), ("203.0.113.11", 9053)])

    def test_only_as_many_peers_as_the_budget_allows_are_asked(self):
        asked = []

        with patch.object(self.nd.sc, "get_peers_id", return_value=["p1", "p2", "p3", "p4"]), \
             patch.object(self.nd, "ask_peer", side_effect=lambda pid, net: asked.append(pid) or []):
            self.nd.ask_peers(self.network, limit=2)

        self.assertEqual(asked, ["p1", "p2"])

    def test_a_budget_of_zero_switches_the_source_off_without_asking_anybody(self):
        with patch.object(self.nd, "ask_peer") as ask:
            self.assertEqual(self.nd.ask_peers(self.network, limit=0), [])

        ask.assert_not_called()

    def test_an_unreadable_peer_list_is_no_sources_rather_than_an_exception(self):
        def _raise():
            raise RuntimeError("database locked")

        with patch.object(self.nd.sc, "get_peers_id", side_effect=_raise):
            self.assertEqual(self.nd.ask_peers(self.network), [])


if __name__ == "__main__":
    unittest.main()

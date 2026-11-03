"""Non-pow domains pool verified peer answers (#619)."""
import sys
import types
import unittest
from unittest.mock import patch

try:
    from tests.config_bootstrap import load_example_config

    load_example_config()

    from protos import celaut_pb2 as celaut
    from tests.test_pow_networks import _load_networks_module

    nets = _load_networks_module()
    IMPORT_ERROR = None if nets is not None else RuntimeError("networks.py did not load")
except Exception as exc:  # pragma: no cover
    IMPORT_ERROR = exc
    nets = None

NET = lambda: celaut.Service.Network(tags=["postgres:nutrition"], formal=b"db.cluster=a")


def _stub(fn):
    module = types.ModuleType("src.manager.network_discovery")
    module.ask_peers = fn
    return module


def _addrs(instances):
    return sorted(nets._addresses_of(instances))


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PeerMembersTests(unittest.TestCase):
    def _resolve(self, suggested, reachable=True, settings=None, ask=True, seeds=None):
        settings = settings or {}
        asked = []

        def fake_ask(network, limit=None):
            asked.append((network, limit))
            return suggested

        with patch.object(nets, "_local_peers", return_value=[]), \
             patch.object(nets, "endpoint_addresses", return_value=seeds or []), \
             patch.object(nets, "hidden_mode", return_value=False), \
             patch.object(nets.env_manager, "get",
                          side_effect=lambda k, d=None: settings.get(k, d)), \
             patch.object(nets, "_reachable", return_value=reachable), \
             patch.dict(sys.modules, {"src.manager.network_discovery": _stub(fake_ask)}):
            result = nets.resolve_network(NET(), ask_peers=ask)
        return result, asked

    def test_a_reachable_public_peer_member_is_returned(self):
        result, asked = self._resolve([("8.8.8.8", 31000, ())])
        self.assertEqual(_addrs(result), [("8.8.8.8", 31000)])
        self.assertEqual(asked[0][0].formal, b"db.cluster=a")

    def test_ask_peers_false_never_asks(self):
        result, asked = self._resolve([("8.8.8.8", 31000, ())], ask=False)
        self.assertEqual(result, [])
        self.assertEqual(asked, [])

    def test_operator_can_switch_it_off(self):
        result, asked = self._resolve(
            [("8.8.8.8", 1, ())], settings={nets.PEER_ASK_KEY: 0})
        self.assertEqual((result, asked), ([], []))

    def test_an_unreachable_address_is_not_offered(self):
        result, _ = self._resolve([("8.8.8.8", 31000, ())], reachable=False)
        self.assertEqual(result, [])

    def test_unacceptable_addresses_are_dropped(self):
        for ip in ("127.0.0.1", "10.0.0.5", "169.254.1.1", "192.168.200.7",
                   "db.example.org", "0.0.0.0"):
            result, _ = self._resolve([(ip, 5432, ())])
            self.assertEqual(result, [], ip)

    def test_private_is_accepted_only_when_the_operator_allows_it(self):
        result, _ = self._resolve(
            [("10.0.0.5", 5432, ())],
            settings={nets.PEER_ALLOW_PRIVATE_KEY: True})
        self.assertEqual(_addrs(result), [("10.0.0.5", 5432)])

    def test_seeds_are_not_duplicated_by_peers(self):
        result, _ = self._resolve(
            [("8.8.8.8", 5432, ()), ("8.8.4.4", 5432, ())], seeds=[("8.8.8.8", 5432)])
        self.assertEqual(_addrs(result), [("8.8.4.4", 5432), ("8.8.8.8", 5432)])

    def test_a_failing_ask_does_not_abort(self):
        with patch.object(nets, "_local_peers", return_value=[]), \
             patch.object(nets, "endpoint_addresses", return_value=[]), \
             patch.object(nets, "hidden_mode", return_value=False), \
             patch.dict(sys.modules, {"src.manager.network_discovery": _stub(
                 lambda network, limit=None: (_ for _ in ()).throw(RuntimeError("x")))}):
            self.assertEqual(nets.resolve_network(NET()), [])


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveForPeerAskTests(unittest.TestCase):
    def _answer(self, caller_ip, declared, requester_id):
        with patch.object(nets, "enforce_network_policy"), \
             patch.object(nets, "enforce_hidden_resolution"), \
             patch.object(nets, "declared_networks_of_caller", return_value=declared), \
             patch.object(nets.sc, "get_local_instance_id_by_uri",
                          return_value=requester_id, create=True), \
             patch.object(nets, "narrow_instances_for_local_grant", side_effect=lambda i, n: i), \
             patch.object(nets, "grant_resolved_network"), \
             patch.object(nets, "resolve_network", return_value=[]) as resolve:
            nets.resolve_network_for_peer(NET(), caller_ip=caller_ip)
        return resolve.call_args.kwargs["ask_peers"]

    def test_a_local_guest_completing_its_network_asks_the_peers(self):
        self.assertIs(self._answer("10.0.0.9", [NET()], "guest-1"), True)

    def test_another_node_asking_is_never_relayed(self):
        self.assertIs(self._answer("1.2.3.4", None, None), False)


if __name__ == "__main__":
    unittest.main()

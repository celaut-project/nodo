"""Unit tests for Service.Network.environment_variable peer filtering."""
import unittest

try:
    from protos import celaut_pb2 as celaut
    from src.manager import network_env as ne
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = None
    ne = None


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class NetworkEnvTest(unittest.TestCase):
    def _net(self, env_var=""):
        return celaut.Service.Network(environment_variable=env_var)

    def test_no_filter_passes_all(self):
        net = self._net("")
        peers = [celaut.Instance(), celaut.Instance()]
        self.assertEqual(
            ne.filter_peers_by_environment(net, peers, None, None), peers
        )

    def test_no_lookup_passes_all(self):
        # e.g. externally resolved DNS peers carry no celaut environment.
        net = self._net("PG_CLUSTER")
        peers = [celaut.Instance(), celaut.Instance()]
        self.assertEqual(
            ne.filter_peers_by_environment(net, peers, {"PG_CLUSTER": b"a"}, None),
            peers,
        )

    def test_only_matching_peers_returned(self):
        net = self._net("PG_CLUSTER")
        p1, p2, p3 = celaut.Instance(), celaut.Instance(), celaut.Instance()
        env = {id(p1): {"PG_CLUSTER": b"a"}, id(p2): {"PG_CLUSTER": b"b"}, id(p3): {"PG_CLUSTER": b"a"}}
        out = ne.filter_peers_by_environment(
            net, [p1, p2, p3], {"PG_CLUSTER": b"a"}, lambda p: env[id(p)]
        )
        self.assertEqual(out, [p1, p3])

    def test_peer_env_matches_semantics(self):
        net = self._net("K")
        self.assertTrue(ne.peer_env_matches(net, {"K": b"v"}, {"K": b"v"}))
        self.assertFalse(ne.peer_env_matches(net, {"K": b"v"}, {"K": b"w"}))
        self.assertFalse(ne.peer_env_matches(net, {"K": b"v"}, None))
        self.assertFalse(ne.peer_env_matches(net, None, {"K": b"v"}))
        # no filter declared -> always matches
        self.assertTrue(ne.peer_env_matches(self._net(""), None, None))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the ``Service.Network`` ancestor-chain authorization control.

``filter_networks_with_ancestors`` decides which communication domains an
instance may use, and it reaches the node database and the service registry
through two module-level seams (``sc`` and ``load_service_from_disk``). Both are
replaced here, so the tests drive the real filtering and recursion over a
synthetic ancestry.

The module is loaded from its file rather than imported by name: importing
``src.manager.networks`` pulls in ``src.database.sql_connection`` and therefore
``bee_rpc``/grpc, which these tests never touch and a minimal checkout does not
have. The stubs that make that load possible are removed from ``sys.modules``
immediately afterwards, so no other test module ever sees them.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

try:
    from protos import celaut_pb2 as celaut
    from src.utils.registry_errors import ServiceNotInRegistry, ServiceSpecUnavailable

    def _load_networks_module():
        stubbed = ("src.database.sql_connection", "src.utils.utils")
        saved = {name: sys.modules.get(name) for name in stubbed}

        sql_stub = types.ModuleType("src.database.sql_connection")
        sql_stub.SQLConnection = type("SQLConnection", (), {})
        utils_stub = types.ModuleType("src.utils.utils")
        utils_stub.load_service_from_disk = lambda service_hash: None
        sys.modules[stubbed[0]] = sql_stub
        sys.modules[stubbed[1]] = utils_stub
        try:
            path = Path(__file__).resolve().parents[1] / "src" / "manager" / "networks.py"
            spec = importlib.util.spec_from_file_location("networks_under_test", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            for name, previous in saved.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

    nw = _load_networks_module()
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = None
    nw = None
    ServiceNotInRegistry = ServiceSpecUnavailable = None


def _net(*tags):
    return celaut.Service.Network(tags=list(tags))


def _spec(*tag_groups):
    """A service spec declaring one Network per group of tags."""
    service = celaut.Service()
    for tags in tag_groups:
        service.network.append(_net(*tags))
    return service


class FakeNode:
    """The three ``SQLConnection`` methods the walk uses, over an in-memory chain.

    ``chain`` maps container id -> (service id, father container id). An empty
    father id, or one absent from the chain, ends the ancestry -- the real
    ``get_internal_father_id`` returns ``""`` for a row with no father.
    """

    def __init__(self, chain):
        self.chain = chain

    def get_service_id_by_container_id(self, id):
        if id not in self.chain:
            # The real method raises rather than returning None (sql_connection.py).
            raise Exception(f"No service found for container ID {id}")
        return self.chain[id][0]

    def get_internal_father_id(self, id):
        return self.chain.get(id, ("", ""))[1]

    def internal_instance_exists(self, id):
        return id in self.chain


class FakeRegistry:
    """A stand-in for ``load_service_from_disk`` that records what it was asked."""

    def __init__(self, specs, failures=None):
        self.specs = specs
        self.failures = failures or {}
        self.reads = []

    def __call__(self, service_hash):
        self.reads.append(service_hash)
        if service_hash in self.failures:
            raise self.failures[service_hash]
        return self.specs[service_hash]


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class MatchNetworksTest(unittest.TestCase):
    def test_returns_a_bool_not_the_intersection(self):
        # It used to return `set(a.tags) & set(b.tags)`, which only worked through
        # truthiness and leaked the tag set to every caller.
        self.assertIs(nw.match_networks(_net("a", "b"), _net("b", "c")), True)
        self.assertIs(nw.match_networks(_net("a"), _net("b")), False)

    def test_no_tags_never_matches(self):
        self.assertIs(nw.match_networks(_net(), _net()), False)
        self.assertIs(nw.match_networks(_net("a"), _net()), False)


class AncestorWalkTestBase(unittest.TestCase):
    """Seam replacement shared by the filtering and the fail-closed cases."""

    def _install(self, chain, specs, failures=None):
        registry = FakeRegistry(specs=specs, failures=failures)
        self._saved = (nw.sc, nw.load_service_from_disk)
        nw.sc = FakeNode(chain)
        nw.load_service_from_disk = registry
        self.addCleanup(self._restore)
        return registry

    def _restore(self):
        nw.sc, nw.load_service_from_disk = self._saved

    @staticmethod
    def _tags(networks):
        return [list(n.tags) for n in networks]


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class FilterNetworksWithAncestorsTest(AncestorWalkTestBase):
    def test_father_keeps_only_what_it_declares(self):
        self._install(
            chain={"child": ("svc-child", "")},
            specs={"svc-child": _spec(["a"], ["b"])},
        )
        out = nw.filter_networks_with_ancestors(
            networks=[_net("a"), _net("b"), _net("c")], father_id="child"
        )
        self.assertEqual(self._tags(out), [["a"], ["b"]])

    def test_a_network_matches_on_any_shared_tag(self):
        self._install(
            chain={"father": ("svc-father", "")},
            specs={"svc-father": _spec(["x", "shared"])},
        )
        out = nw.filter_networks_with_ancestors(
            networks=[_net("unrelated", "shared")], father_id="father"
        )
        self.assertEqual(self._tags(out), [["unrelated", "shared"]])

    def test_the_whole_chain_is_anded(self):
        self._install(
            chain={
                "father": ("svc-father", "grandfather"),
                "grandfather": ("svc-grandfather", "root"),
                "root": ("svc-root", ""),
            },
            specs={
                "svc-father": _spec(["a"], ["b"], ["c"]),
                "svc-grandfather": _spec(["a"], ["b"]),
                "svc-root": _spec(["b"]),
            },
        )
        out = nw.filter_networks_with_ancestors(
            networks=[_net("a"), _net("b"), _net("c")], father_id="father"
        )
        self.assertEqual(self._tags(out), [["b"]])

    def test_a_grandfather_veto_beats_the_father_grant(self):
        registry = self._install(
            chain={"father": ("svc-father", "grandfather"), "grandfather": ("svc-gf", "")},
            specs={"svc-father": _spec(["a"]), "svc-gf": _spec(["z"])},
        )
        out = nw.filter_networks_with_ancestors(networks=[_net("a")], father_id="father")
        self.assertEqual(out, [])
        self.assertEqual(registry.reads, ["svc-father", "svc-gf"])

    def test_walk_stops_at_an_ancestor_that_is_not_a_local_instance(self):
        # get_internal_father_id returns an id the node does not know (e.g. the
        # remote client that asked for the father): the recursion ends there.
        registry = self._install(
            chain={"father": ("svc-father", "some-remote-client")},
            specs={"svc-father": _spec(["a"])},
        )
        out = nw.filter_networks_with_ancestors(networks=[_net("a")], father_id="father")
        self.assertEqual(self._tags(out), [["a"]])
        self.assertEqual(registry.reads, ["svc-father"])

    def test_no_requested_network_reads_no_spec(self):
        registry = self._install(
            chain={"father": ("svc-father", "")}, specs={"svc-father": _spec(["a"])}
        )
        self.assertEqual(
            nw.filter_networks_with_ancestors(networks=[], father_id="father"), []
        )
        self.assertEqual(registry.reads, [])

    def test_an_emptied_grant_stops_the_walk_early(self):
        registry = self._install(
            chain={"father": ("svc-father", "grandfather"), "grandfather": ("svc-gf", "")},
            specs={"svc-father": _spec(["z"]), "svc-gf": _spec(["a"])},
        )
        out = nw.filter_networks_with_ancestors(networks=[_net("a")], father_id="father")
        self.assertEqual(out, [])
        # Nothing is left to authorize, so the grandfather's spec is never read.
        self.assertEqual(registry.reads, ["svc-father"])


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class UnreadableSpecFailsClosedTest(AncestorWalkTestBase):
    """#269: an unreadable ancestor spec must never grant.

    The old code returned the caller's list unfiltered, which skipped that
    generation's check *and* every ancestor above it, because the return happened
    before the recursion.
    """

    def test_memory_pressure_at_the_father_aborts_instead_of_granting(self):
        # The load-dependent branch: read_service_from_disk answered None on a
        # TimeoutError waiting to unlock memory, so the control degraded to
        # allow-all precisely when the node was busy.
        self._install(
            chain={"father": ("svc-father", "")},
            specs={},
            failures={"svc-father": ServiceSpecUnavailable("timed out unlocking memory")},
        )
        with self.assertRaises(nw.NetworkAuthorizationError) as ctx:
            nw.filter_networks_with_ancestors(networks=[_net("a")], father_id="father")
        self.assertIsInstance(ctx.exception.__cause__, ServiceSpecUnavailable)

    def test_a_spec_missing_from_the_registry_aborts_too(self):
        # The father is a local instance, so this node launched it from a spec it
        # had stored: a missing spec is an inconsistent registry, and there is no
        # way to re-derive what that generation was allowed to reach.
        self._install(
            chain={"father": ("svc-father", "")},
            specs={},
            failures={"svc-father": ServiceNotInRegistry("not on the local registry")},
        )
        with self.assertRaises(nw.NetworkAuthorizationError) as ctx:
            nw.filter_networks_with_ancestors(networks=[_net("a")], father_id="father")
        self.assertIsInstance(ctx.exception.__cause__, ServiceNotInRegistry)

    def test_an_unreadable_grandfather_aborts_a_father_approved_network(self):
        # The father does grant "a"; the grandfather's spec cannot be read. Passing
        # the father's filtered list through was the fail-open that skipped every
        # check above the failure.
        self._install(
            chain={"father": ("svc-father", "grandfather"), "grandfather": ("svc-gf", "")},
            specs={"svc-father": _spec(["a"])},
            failures={"svc-gf": ServiceSpecUnavailable("timed out unlocking memory")},
        )
        with self.assertRaises(nw.NetworkAuthorizationError):
            nw.filter_networks_with_ancestors(networks=[_net("a")], father_id="father")


if __name__ == "__main__":
    unittest.main()

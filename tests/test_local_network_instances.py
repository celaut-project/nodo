"""Local instances as network members (#387, docs/NETWORKS.md "Network Instance Indexing").

``resolve_network`` used to know three sources for a generic tag: the operator's
seeds, DNS, and nothing else. An instance this node was itself running -- one whose
service declared the network *and* exposed its ``protocol_stack`` on an API slot --
was never offered, although the documentation had described exactly that rule as
how a node indexes members. These tests drive ``local_network_instances`` and the
way ``resolve_network`` folds its answer in, over a stubbed registry.

The module is loaded from its file the way ``test_networks_ancestors.py`` does it,
so the database and registry seams (``sc``, ``load_service_from_disk``) are plain
attributes to patch and no sqlite table has to exist.
"""
import unittest
from unittest.mock import patch

try:
    from tests.config_bootstrap import load_example_config

    load_example_config()

    from protos import celaut_pb2 as celaut
    from tests.test_pow_networks import _load_networks_module

    nets = _load_networks_module()
    IMPORT_ERROR = None if nets is not None else RuntimeError("networks.py did not load")
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = None
    nets = None


def _protocol(*tags, formal=b""):
    return celaut.Service.Api.Protocol(tags=list(tags), formal=formal)


def _network(tags=("postgres",), protocol_stack=()):
    return celaut.Service.Network(
        tags=list(tags),
        protocol_stack=list(protocol_stack),
    )


def _spec(networks=(), slots=()):
    """A service declaring ``networks`` and exposing ``slots`` as (port, stack)."""
    spec = celaut.Service()
    for network in networks:
        spec.network.add().CopyFrom(network)
    for port, stack in slots:
        spec.api.slot.add(port=port, protocol_stack=list(stack))
    return spec


def _stored(*uri_slots, contracts=()):
    """The ``Instance`` the launcher records: (internal_port, [(ip, port)...])."""
    instance = celaut.Instance()
    for internal, uris in uri_slots:
        slot = instance.uri_slot.add(internal_port=internal)
        for ip, port in uris:
            slot.uri.add(ip=ip, port=port)
    return instance.SerializeToString()


class _Registry:
    """``sc`` and ``load_service_from_disk`` over a dict of fake instances."""

    def __init__(self, instances):
        # id -> dict(spec=Service, stored=bytes|None)
        self.instances = instances

    def get_all_internal_containers_ids(self):
        return list(self.instances)

    def get_service_id_by_container_id(self, id):
        if id not in self.instances:
            raise Exception(f"No service found for container ID {id}")
        return f"svc-{id}"

    def get_internal_instance(self, id):
        return self.instances[id].get("stored")

    def get_local_instance_id_by_uri(self, uri):
        for instance_id, row in self.instances.items():
            if row.get("ip") == uri:
                return instance_id
        return None

    def load_service_from_disk(self, service_hash):
        instance_id = service_hash[len("svc-"):]
        spec = self.instances[instance_id]["spec"]
        if isinstance(spec, Exception):
            raise spec
        return spec


PG = _protocol("postgres-wire", "pgwire")
HTTP = _protocol("http")


def _member(spec, stored=None, ip=None):
    return {"spec": spec, "stored": stored, "ip": ip}


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LocalNetworkInstancesTests(unittest.TestCase):
    def _members(self, registry, network, requester_id=None):
        with patch.object(nets, "sc", registry), patch.object(
            nets, "load_service_from_disk", registry.load_service_from_disk
        ):
            return nets.local_network_instances(network, requester_id=requester_id)

    def test_an_instance_declaring_and_exposing_the_network_is_a_member(self):
        registry = _Registry({
            "pg-1": _member(
                _spec(networks=[_network(("postgres",), [PG])], slots=[(5432, [PG])]),
                stored=_stored((5432, [("10.0.0.5", 31000)])),
            ),
        })
        members = self._members(registry, _network(("postgres",), [PG]))
        self.assertEqual([i for i, _ in members], ["pg-1"])
        instance = members[0][1]
        self.assertEqual(
            [(u.ip, u.port) for u in instance.uri_slot[0].uri], [("10.0.0.5", 31000)]
        )
        self.assertEqual(instance.uri_slot[0].internal_port, 5432)
        self.assertEqual(list(instance.api.slot[0].protocol_stack[0].tags), list(PG.tags))

    def test_declaring_the_network_without_exposing_its_protocols_is_consumer_only(self):
        """Condition 2 of the documented rule: wanting peers is not being one."""
        registry = _Registry({
            "client": _member(
                _spec(networks=[_network(("postgres",), [PG])], slots=[(8080, [HTTP])]),
                stored=_stored((8080, [("10.0.0.6", 31001)])),
            ),
        })
        self.assertEqual(self._members(registry, _network(("postgres",), [PG])), [])

    def test_exposing_the_protocols_without_declaring_the_network_is_not_membership(self):
        """Condition 1: a service speaking pgwire for its own reasons has not joined."""
        registry = _Registry({
            "lonely-pg": _member(
                _spec(networks=[], slots=[(5432, [PG])]),
                stored=_stored((5432, [("10.0.0.7", 31002)])),
            ),
        })
        self.assertEqual(self._members(registry, _network(("postgres",), [PG])), [])

    def test_a_network_with_no_protocol_stack_is_satisfied_by_any_slot(self):
        registry = _Registry({
            "svc": _member(
                _spec(networks=[_network(("mesh",))], slots=[(9000, [HTTP])]),
                stored=_stored((9000, [("10.0.0.8", 31003)])),
            ),
        })
        members = self._members(registry, _network(("mesh",)))
        self.assertEqual([i for i, _ in members], ["svc"])

    def test_only_the_qualifying_slots_are_offered(self):
        """An admin port on the same instance is not opened to a postgres peer."""
        registry = _Registry({
            "pg-1": _member(
                _spec(
                    networks=[_network(("postgres",), [PG])],
                    slots=[(5432, [PG]), (9100, [HTTP])],
                ),
                stored=_stored(
                    (5432, [("10.0.0.5", 31000)]), (9100, [("10.0.0.5", 31099)])
                ),
            ),
        })
        [(_, instance)] = self._members(registry, _network(("postgres",), [PG]))
        self.assertEqual([s.internal_port for s in instance.uri_slot], [5432])
        self.assertEqual([s.port for s in instance.api.slot], [5432])

    def test_a_member_with_no_advertised_address_is_skipped(self):
        registry = _Registry({
            "pg-1": _member(
                _spec(networks=[_network(("postgres",), [PG])], slots=[(5432, [PG])]),
                stored=_stored((5432, [])),
            ),
        })
        self.assertEqual(self._members(registry, _network(("postgres",), [PG])), [])

    def test_a_member_still_booting_has_no_definition_and_is_skipped(self):
        registry = _Registry({
            "pg-1": _member(
                _spec(networks=[_network(("postgres",), [PG])], slots=[(5432, [PG])]),
                stored=None,
            ),
        })
        self.assertEqual(self._members(registry, _network(("postgres",), [PG])), [])

    def test_the_requester_is_not_its_own_peer(self):
        spec = _spec(networks=[_network(("postgres",), [PG])], slots=[(5432, [PG])])
        registry = _Registry({
            "pg-1": _member(spec, stored=_stored((5432, [("10.0.0.5", 31000)]))),
            "pg-2": _member(spec, stored=_stored((5432, [("10.0.0.9", 31001)]))),
        })
        members = self._members(registry, _network(("postgres",), [PG]), requester_id="pg-2")
        self.assertEqual([i for i, _ in members], ["pg-1"])

    def test_an_unreadable_spec_skips_that_instance_and_keeps_the_others(self):
        good = _spec(networks=[_network(("postgres",), [PG])], slots=[(5432, [PG])])
        registry = _Registry({
            "broken": _member(OSError("locked")),
            "pg-1": _member(good, stored=_stored((5432, [("10.0.0.5", 31000)]))),
        })
        with patch.object(nets, "LOGGER") as logger:
            members = self._members(registry, _network(("postgres",), [PG]))
        self.assertEqual([i for i, _ in members], ["pg-1"])
        self.assertTrue(logger.called)

    def test_membership_is_judged_by_match_networks(self):
        """A shared tag is enough; a differing formal on both sides is not."""
        declared = celaut.Service.Network(tags=["postgres", "pg"], formal=b"a=1")
        registry = _Registry({
            "pg-1": _member(
                _spec(networks=[declared], slots=[(5432, [PG])]),
                stored=_stored((5432, [("10.0.0.5", 31000)])),
            ),
        })
        by_tag = celaut.Service.Network(tags=["pg"], protocol_stack=[PG])
        self.assertEqual(len(self._members(registry, by_tag)), 1)
        other_formal = celaut.Service.Network(tags=["pg"], protocol_stack=[PG], formal=b"a=2")
        self.assertEqual(self._members(registry, other_formal), [])


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveNetworkWithLocalMembersTests(unittest.TestCase):
    def setUp(self):
        self.spec = _spec(networks=[_network(("postgres",), [PG])], slots=[(5432, [PG])])

    def _resolve(self, registry, network, **kwargs):
        with patch.object(nets, "sc", registry), patch.object(
            nets, "load_service_from_disk", registry.load_service_from_disk
        ), patch.object(nets, "resolve_domain", return_value=[]):
            return nets.resolve_network(network, **kwargs)

    def test_a_generic_tag_with_no_seeds_and_no_dns_answers_with_local_members(self):
        registry = _Registry({
            "pg-1": _member(self.spec, stored=_stored((5432, [("10.0.0.5", 31000)]))),
        })
        peers = self._resolve(registry, _network(("postgres",), [PG]))
        self.assertEqual([p.uri_slot[0].uri[0].ip for p in peers], ["10.0.0.5"])

    def test_local_members_are_offered_alongside_operator_seeds(self):
        from src.manager import network_defaults as defaults

        registry = _Registry({
            "pg-1": _member(self.spec, stored=_stored((5432, [("10.0.0.5", 31000)]))),
        })
        with patch.object(
            nets, "configured_endpoints", return_value=["tcp://203.0.113.9:5432"]
        ), patch.object(
            defaults.socket, "getaddrinfo",
            side_effect=lambda host, port, *a: [(None, None, None, None, (host, port))],
        ):
            peers = self._resolve(registry, _network(("postgres",), [PG]))
        self.assertEqual(
            [p.uri_slot[0].uri[0].ip for p in peers], ["10.0.0.5", "203.0.113.9"]
        )

    def test_local_members_are_offered_alongside_dns_peers(self):
        registry = _Registry({
            "pg-1": _member(
                _spec(networks=[_network(("db.example.test",), [PG])], slots=[(5432, [PG])]),
                stored=_stored((5432, [("10.0.0.5", 31000)])),
            ),
        })
        with patch.object(nets, "sc", registry), patch.object(
            nets, "load_service_from_disk", registry.load_service_from_disk
        ), patch.object(
            nets, "resolve_domain", return_value=[celaut.Instance.Uri(ip="198.51.100.1", port=443)]
        ):
            peers = nets.resolve_network(_network(("db.example.test",), [PG]))
        self.assertEqual(
            [p.uri_slot[0].uri[0].ip for p in peers], ["10.0.0.5", "198.51.100.1"]
        )

    def test_a_pow_network_never_takes_local_members(self):
        """Membership of a PoW domain is verified chain state, not a declaration."""
        registry = _Registry({
            "ergo-node": _member(
                _spec(networks=[_network(("pow:ergo",))], slots=[(9053, [HTTP])]),
                stored=_stored((9053, [("10.0.0.5", 31000)])),
            ),
        })
        with patch.object(nets, "sc", registry), patch.object(
            nets, "load_service_from_disk", registry.load_service_from_disk
        ), patch.object(nets, "resolve_pow_network", return_value=[]), patch.object(
            nets, "local_network_instances"
        ) as index:
            self.assertEqual(nets.resolve_network(_network(("pow:ergo",))), [])
        index.assert_not_called()

    def test_the_wildcard_tag_still_resolves_to_nothing_on_an_idle_node(self):
        self.assertEqual(self._resolve(_Registry({}), _network(("*",))), [])


@unittest.skipIf(IMPORT_ERROR, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveForPeerExcludesTheCallerTests(unittest.TestCase):
    def test_a_local_caller_is_answered_with_the_other_members_only(self):
        network = _network(("postgres",), [PG])
        spec = _spec(networks=[network], slots=[(5432, [PG])])
        registry = _Registry({
            "pg-1": _member(spec, stored=_stored((5432, [("10.0.0.5", 31000)])), ip="10.0.0.5"),
            "pg-2": _member(spec, stored=_stored((5432, [("10.0.0.9", 31001)])), ip="10.0.0.9"),
        })
        with patch.object(nets, "sc", registry), patch.object(
            nets, "load_service_from_disk", registry.load_service_from_disk
        ), patch.object(nets, "resolve_domain", return_value=[]):
            resolution = nets.resolve_network_for_peer(network, caller_ip="10.0.0.9")
        self.assertEqual(
            [p.uri_slot[0].uri[0].ip for p in resolution.peer_instances], ["10.0.0.5"]
        )


if __name__ == "__main__":
    unittest.main()

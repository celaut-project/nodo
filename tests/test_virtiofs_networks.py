import hashlib
import unittest

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.utils import networks
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    networks = None  # type: ignore[assignment]


ABCD = b"ABCD-anchor-blob"


def _virtiofs_network(anchor: bytes = ABCD, tags=("shared-disk",), protocol_tags=("virtiofs",)):
    return celaut.Service.Network(
        tags=list(tags),
        formal=anchor,
        protocol_stack=[celaut.Service.Api.Protocol(tags=list(protocol_tags))],
    )


def _http_network(anchor: bytes = b"http-anchor"):
    return celaut.Service.Network(
        formal=anchor,
        protocol_stack=[
            celaut.Service.Api.Protocol(tags=["http"]),
            celaut.Service.Api.Protocol(tags=["tcp"]),
        ],
    )


def _service(networks_list):
    return celaut.Service(network=list(networks_list))


def _instance(ip="10.0.0.9", port=5000):
    return celaut.Instance(
        uri_slot=[celaut.Instance.Uri_Slot(internal_port=port, uri=[celaut.Instance.Uri(ip=ip, port=port)])]
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NetworkIdentityTests(unittest.TestCase):
    def test_content_id_is_hash_of_anchor(self):
        net = _virtiofs_network(anchor=ABCD)
        self.assertEqual(networks.network_content_id(net), hashlib.sha256(ABCD).digest())

    def test_same_anchor_same_id_regardless_of_prose(self):
        a = _virtiofs_network(anchor=ABCD)
        b = _virtiofs_network(anchor=ABCD)
        b.prose = "a different human description"
        self.assertEqual(networks.network_content_id(a), networks.network_content_id(b))

    def test_different_anchor_different_id(self):
        self.assertNotEqual(
            networks.network_content_id(_virtiofs_network(anchor=b"AAAA")),
            networks.network_content_id(_virtiofs_network(anchor=b"BBBB")),
        )

    def test_fallback_id_is_deterministic_without_anchor(self):
        net = celaut.Service.Network(
            tags=["shared-disk"],
            protocol_stack=[celaut.Service.Api.Protocol(tags=["virtiofs"])],
        )
        self.assertEqual(networks.network_content_id(net), networks.network_content_id(net))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class VirtiofsDetectionTests(unittest.TestCase):
    def test_virtiofs_network_detected(self):
        self.assertTrue(networks.is_virtiofs_network(_virtiofs_network()))

    def test_virtiofs_detected_case_insensitive_and_aliases(self):
        for tag in ("VirtioFS", "virtio-fs", "virtiofsd", "virtio_fs"):
            net = celaut.Service.Network(protocol_stack=[celaut.Service.Api.Protocol(tags=[tag])])
            self.assertTrue(networks.is_virtiofs_network(net), tag)

    def test_http_network_is_not_virtiofs(self):
        self.assertFalse(networks.is_virtiofs_network(_http_network()))

    def test_only_virtiofs_filter_on_declared_ids(self):
        svc = _service([_virtiofs_network(), _http_network()])
        virtiofs_ids = networks.declared_network_ids(svc, only_virtiofs=True)
        all_ids = networks.declared_network_ids(svc)
        self.assertEqual(len(virtiofs_ids), 1)
        self.assertEqual(len(all_ids), 2)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReadOnlyDeclarationTests(unittest.TestCase):
    """Read-only shared disk is declared with a reserved tag — no proto change."""

    def test_default_network_is_read_write(self):
        self.assertFalse(networks.network_is_readonly(_virtiofs_network()))

    def test_readonly_via_network_tag(self):
        net = _virtiofs_network(tags=("shared-disk", "readonly"))
        self.assertTrue(networks.network_is_readonly(net))

    def test_readonly_via_protocol_tag_and_aliases(self):
        for tag in ("readonly", "read-only", "read_only", "ro", "RO"):
            net = _virtiofs_network(protocol_tags=("virtiofs", tag))
            self.assertTrue(networks.network_is_readonly(net), tag)

    def test_readonly_does_not_change_network_identity(self):
        writer = _virtiofs_network(anchor=ABCD, tags=("shared-disk",))
        reader = _virtiofs_network(anchor=ABCD, tags=("shared-disk", "readonly"))
        # Reader and writer resolve to the SAME network despite the ro tag.
        self.assertEqual(
            networks.network_content_id(writer), networks.network_content_id(reader)
        )
        self.assertTrue(networks.network_is_readonly(reader))
        self.assertFalse(networks.network_is_readonly(writer))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DeclaredNetworksHelperTests(unittest.TestCase):
    """'List my declared networks' helper."""

    def test_lists_all_networks_with_flags(self):
        vfs = _virtiofs_network(anchor=ABCD)
        ro = _virtiofs_network(anchor=b"other", tags=("shared-disk", "ro"))
        http = _http_network()
        summary = networks.declared_networks(_service([vfs, ro, http]))
        self.assertEqual(len(summary), 3)
        by_id = {d.network_id: d for d in summary}
        self.assertTrue(by_id[networks.network_content_id(vfs)].virtiofs)
        self.assertFalse(by_id[networks.network_content_id(vfs)].readonly)
        self.assertTrue(by_id[networks.network_content_id(ro)].readonly)
        self.assertFalse(by_id[networks.network_content_id(http)].virtiofs)

    def test_only_virtiofs_filter(self):
        summary = networks.declared_networks(
            _service([_virtiofs_network(), _http_network()]), only_virtiofs=True
        )
        self.assertEqual(len(summary), 1)
        self.assertTrue(summary[0].virtiofs)

    def test_deduplicates_by_content_id(self):
        net = _virtiofs_network(anchor=ABCD)
        summary = networks.declared_networks(_service([net, _virtiofs_network(anchor=ABCD)]))
        self.assertEqual(len(summary), 1)

    def test_hex_matches_content_id(self):
        net = _virtiofs_network(anchor=ABCD)
        [d] = networks.declared_networks(_service([net]))
        self.assertEqual(d.network_id_hex, networks.network_content_id(net).hex())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AuthorizationGateTests(unittest.TestCase):
    """The exact predicate the GetNetworkInstances handler uses to authorize a caller."""

    def test_caller_declaring_network_is_authorized(self):
        net = _virtiofs_network()
        svc = _service([net])
        self.assertTrue(networks.service_declares_network(svc, networks.network_content_id(net)))

    def test_caller_not_declaring_network_is_denied(self):
        svc = _service([_virtiofs_network(anchor=b"mine")])
        other_id = networks.network_content_id(_virtiofs_network(anchor=b"someone-elses"))
        self.assertFalse(networks.service_declares_network(svc, other_id))

    def test_empty_network_id_is_denied(self):
        svc = _service([_virtiofs_network()])
        self.assertFalse(networks.service_declares_network(svc, b""))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LocalMembershipTests(unittest.TestCase):
    def _rows_and_loader(self):
        net = _virtiofs_network(anchor=ABCD)
        member_service = _service([net])
        outsider_service = _service([_http_network()])
        services = {"svc-member": member_service, "svc-outsider": outsider_service}
        rows = [
            {"id": "i1", "service_id": "svc-member", "serialized_instance": _instance("10.0.0.1").SerializeToString()},
            {"id": "i2", "service_id": "svc-outsider", "serialized_instance": _instance("10.0.0.2").SerializeToString()},
            {"id": "i3", "service_id": "svc-member", "serialized_instance": _instance("10.0.0.3").SerializeToString()},
        ]
        return net, rows, (lambda sid: services.get(sid))

    def test_finds_only_members_of_the_network(self):
        net, rows, loader = self._rows_and_loader()
        found = networks.find_local_network_instances(
            networks.network_content_id(net), local_rows=rows, load_service=loader
        )
        ips = sorted(i.uri_slot[0].uri[0].ip for i in found)
        self.assertEqual(ips, ["10.0.0.1", "10.0.0.3"])

    def test_local_node_hosts_network_true_and_false(self):
        net, rows, loader = self._rows_and_loader()
        self.assertTrue(networks.local_node_hosts_network(
            networks.network_content_id(net), local_rows=rows, load_service=loader))
        absent = networks.network_content_id(_virtiofs_network(anchor=b"absent"))
        self.assertFalse(networks.local_node_hosts_network(
            absent, local_rows=rows, load_service=loader))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PlacementGatingTests(unittest.TestCase):
    def _cost(self):
        return celaut.EstimatedCost()

    def test_non_virtiofs_service_placement_unchanged(self):
        svc = _service([_http_network()])
        peers = {"local": self._cost(), "peerA": self._cost()}
        out = networks.filter_placements_for_colocation(
            svc, peers, local_hosts_network=lambda _nid: False
        )
        self.assertEqual(set(out), {"local", "peerA"})

    def test_virtiofs_already_local_pins_local(self):
        svc = _service([_virtiofs_network()])
        peers = {"local": self._cost(), "peerA": self._cost(), "peerB": self._cost()}
        out = networks.filter_placements_for_colocation(
            svc, peers, local_hosts_network=lambda _nid: True
        )
        self.assertEqual(set(out), {"local"})

    def test_virtiofs_seed_prefers_local_and_drops_peers(self):
        svc = _service([_virtiofs_network()])
        peers = {"local": self._cost(), "peerA": self._cost()}
        out = networks.filter_placements_for_colocation(
            svc, peers, local_hosts_network=lambda _nid: False
        )
        self.assertEqual(set(out), {"local"})

    def test_virtiofs_without_local_capacity_yields_no_placement(self):
        svc = _service([_virtiofs_network()])
        peers = {"peerA": self._cost(), "peerB": self._cost()}
        messages = []
        out = networks.filter_placements_for_colocation(
            svc, peers, local_hosts_network=lambda _nid: False, logger_fn=messages.append
        )
        self.assertEqual(out, {})
        self.assertTrue(any("co-location" in m.lower() for m in messages))

    def test_input_dict_not_mutated(self):
        svc = _service([_virtiofs_network()])
        peers = {"local": self._cost(), "peerA": self._cost()}
        networks.filter_placements_for_colocation(
            svc, peers, local_hosts_network=lambda _nid: True
        )
        self.assertEqual(set(peers), {"local", "peerA"})

    def test_local_hosting_beats_remote_hosting(self):
        # If the local node hosts the network it wins even when a peer hosts it.
        svc = _service([_virtiofs_network()])
        peers = {"local": self._cost(), "peerA": self._cost()}
        out = networks.filter_placements_for_colocation(
            svc, peers,
            local_hosts_network=lambda _nid: True,
            remote_hosts_network=lambda _pid, _nid: True,
        )
        self.assertEqual(set(out), {"local"})

    def test_distributed_seeding_routes_to_hosting_peer(self):
        # Local doesn't host it, peerB does -> co-locate on peerB, drop the rest.
        svc = _service([_virtiofs_network()])
        peers = {"local": self._cost(), "peerA": self._cost(), "peerB": self._cost()}
        out = networks.filter_placements_for_colocation(
            svc, peers,
            local_hosts_network=lambda _nid: False,
            remote_hosts_network=lambda pid, _nid: pid == "peerB",
        )
        self.assertEqual(set(out), {"peerB"})

    def test_distributed_seeding_can_place_without_local(self):
        # No local candidate at all, but a peer hosts the network: placeable.
        svc = _service([_virtiofs_network()])
        peers = {"peerA": self._cost(), "peerB": self._cost()}
        out = networks.filter_placements_for_colocation(
            svc, peers,
            local_hosts_network=lambda _nid: False,
            remote_hosts_network=lambda pid, _nid: pid == "peerA",
        )
        self.assertEqual(set(out), {"peerA"})

    def test_no_host_anywhere_falls_back_to_local_seed(self):
        svc = _service([_virtiofs_network()])
        peers = {"local": self._cost(), "peerA": self._cost()}
        out = networks.filter_placements_for_colocation(
            svc, peers,
            local_hosts_network=lambda _nid: False,
            remote_hosts_network=lambda _pid, _nid: False,
        )
        self.assertEqual(set(out), {"local"})

    def test_multi_network_requires_a_single_node_hosting_all(self):
        # Two virtiofs disks; peerA hosts only one -> not a valid target; seed local.
        net1 = _virtiofs_network(anchor=b"one")
        net2 = _virtiofs_network(anchor=b"two")
        id1 = networks.network_content_id(net1)
        svc = _service([net1, net2])
        peers = {"local": self._cost(), "peerA": self._cost()}
        out = networks.filter_placements_for_colocation(
            svc, peers,
            local_hosts_network=lambda _nid: False,
            remote_hosts_network=lambda pid, nid: pid == "peerA" and nid == id1,
        )
        self.assertEqual(set(out), {"local"})


if __name__ == "__main__":
    unittest.main()

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


def _virtiofs_network(anchor: bytes = ABCD, tags=("shared-disk",)):
    return celaut.Service.Network(
        tags=list(tags),
        formal=anchor,
        protocol_stack=[celaut.Service.Api.Protocol(tags=["virtiofs"])],
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


if __name__ == "__main__":
    unittest.main()

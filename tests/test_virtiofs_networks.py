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


def _virtiofs_network(
    anchor: bytes = ABCD, tags=("shared-disk",), protocol_tags=("virtiofs",), handle="@disk"
):
    # A virtiofs network's identity now comes from a required "@handle" tag on
    # its own tags (not H(formal)); append it unless a test opts out (handle=None).
    all_tags = list(tags)
    if handle is not None:
        all_tags.append(handle)
    return celaut.Service.Network(
        tags=all_tags,
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
    """A virtiofs network's identity is derived from its explicit @handle tag."""

    def test_virtiofs_id_is_hash_of_handle_not_formal(self):
        net = _virtiofs_network(anchor=ABCD, handle="@photos-nate")
        expected = hashlib.sha256(
            networks.VIRTIOFS_DISK_ID_PREFIX + b"@photos-nate"
        ).digest()
        self.assertEqual(networks.network_content_id(net), expected)
        # NOT the legacy H(formal).
        self.assertNotEqual(networks.network_content_id(net), hashlib.sha256(ABCD).digest())

    def test_same_handle_different_formal_same_id(self):
        # (a) The whole point: same @handle, different anchor -> SAME network id.
        a = _virtiofs_network(anchor=b"AAAA", handle="@shared")
        b = _virtiofs_network(anchor=b"BBBB", handle="@shared")
        self.assertEqual(networks.network_content_id(a), networks.network_content_id(b))

    def test_same_handle_same_id_regardless_of_prose(self):
        a = _virtiofs_network(anchor=ABCD, handle="@shared")
        b = _virtiofs_network(anchor=ABCD, handle="@shared")
        b.prose = "a different human description"
        self.assertEqual(networks.network_content_id(a), networks.network_content_id(b))

    def test_different_handle_different_id(self):
        # (b) Different @handle -> different id, even with identical formal.
        self.assertNotEqual(
            networks.network_content_id(_virtiofs_network(anchor=ABCD, handle="@one")),
            networks.network_content_id(_virtiofs_network(anchor=ABCD, handle="@two")),
        )

    def test_handle_is_case_insensitive_like_other_tags(self):
        self.assertEqual(
            networks.network_content_id(_virtiofs_network(handle="@Photos")),
            networks.network_content_id(_virtiofs_network(handle="@photos")),
        )

    def test_missing_handle_is_invalid(self):
        # (c) A virtiofs network with zero @handle tags is invalid.
        net = _virtiofs_network(handle=None)
        with self.assertRaises(ValueError):
            networks.network_content_id(net)

    def test_multiple_distinct_handles_is_invalid(self):
        # (d) More than one distinct @handle on one network is invalid.
        net = _virtiofs_network(tags=("shared-disk", "@a", "@b"), handle=None)
        with self.assertRaises(ValueError):
            networks.network_content_id(net)

    def test_repeated_same_handle_is_valid(self):
        # Same @handle listed twice collapses to one distinct handle -> valid.
        net = _virtiofs_network(tags=("shared-disk", "@dup", "@dup"), handle=None)
        self.assertEqual(networks.network_identity_handle(net), "@dup")

    def test_non_virtiofs_network_keeps_formal_identity(self):
        # Non-virtiofs networks are unaffected: still H(formal), no @handle needed.
        net = _http_network(anchor=b"http-anchor")
        self.assertEqual(
            networks.network_content_id(net), hashlib.sha256(b"http-anchor").digest()
        )


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
        vfs = _virtiofs_network(anchor=ABCD, handle="@vfs")
        ro = _virtiofs_network(anchor=b"other", tags=("shared-disk", "ro"), handle="@ro-disk")
        http = _http_network()
        summary = networks.declared_networks(_service([vfs, ro, http]))
        self.assertEqual(len(summary), 3)
        by_id = {d.network_id: d for d in summary}
        self.assertTrue(by_id[networks.network_content_id(vfs)].virtiofs)
        self.assertFalse(by_id[networks.network_content_id(vfs)].readonly)
        self.assertTrue(by_id[networks.network_content_id(ro)].readonly)
        self.assertFalse(by_id[networks.network_content_id(http)].virtiofs)
        # The raw @handle is surfaced as a human-readable field on virtiofs nets.
        self.assertEqual(by_id[networks.network_content_id(vfs)].disk_handle, "@vfs")
        self.assertEqual(by_id[networks.network_content_id(ro)].disk_handle, "@ro-disk")
        self.assertEqual(by_id[networks.network_content_id(http)].disk_handle, "")

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
        svc = _service([_virtiofs_network(anchor=b"mine", handle="@mine")])
        other_id = networks.network_content_id(
            _virtiofs_network(anchor=b"someone-elses", handle="@theirs")
        )
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
        absent = networks.network_content_id(_virtiofs_network(anchor=b"absent", handle="@absent"))
        self.assertFalse(networks.local_node_hosts_network(
            absent, local_rows=rows, load_service=loader))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuestDeclarationTests(unittest.TestCase):
    """Guest membership: 'never seed, only join where the network exists'."""

    def test_default_network_is_seed_not_guest(self):
        self.assertFalse(networks.network_is_guest(_virtiofs_network()))

    def test_guest_via_network_tag(self):
        self.assertTrue(networks.network_is_guest(_virtiofs_network(tags=("shared-disk", "guest"))))

    def test_guest_via_protocol_tag_case_insensitive(self):
        self.assertTrue(networks.network_is_guest(_virtiofs_network(protocol_tags=("virtiofs", "GUEST"))))

    def test_guest_does_not_change_identity(self):
        seed = _virtiofs_network(anchor=ABCD, tags=("shared-disk",))
        guest = _virtiofs_network(anchor=ABCD, tags=("shared-disk", "guest"))
        self.assertEqual(networks.network_content_id(seed), networks.network_content_id(guest))

    def test_declared_networks_surfaces_guest_flag(self):
        [d] = networks.declared_networks(_service([_virtiofs_network(tags=("shared-disk", "guest"))]))
        self.assertTrue(d.guest)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class NodeAdmissibilityTests(unittest.TestCase):
    """
    Whether a node can host a service given its virtiofs networks. This is the
    self-check each node applies (locally and when a peer asks for its cost).
    """

    def test_non_virtiofs_service_always_admissible(self):
        svc = _service([_http_network()])
        self.assertTrue(networks.node_can_host_service(svc, lambda _nid: False))

    def test_seed_network_admissible_anywhere(self):
        # No guest tag -> node may create the disk -> admissible even if absent.
        svc = _service([_virtiofs_network()])
        self.assertTrue(networks.node_can_host_service(svc, lambda _nid: False))
        self.assertTrue(networks.node_can_host_service(svc, lambda _nid: True))

    def test_guest_network_requires_local_presence(self):
        svc = _service([_virtiofs_network(tags=("shared-disk", "guest"))])
        self.assertFalse(networks.node_can_host_service(svc, lambda _nid: False))
        self.assertTrue(networks.node_can_host_service(svc, lambda _nid: True))

    def test_guest_only_checks_its_own_network(self):
        # Guest network present locally, but only THAT id counts.
        guest_net = _virtiofs_network(anchor=b"g", tags=("shared-disk", "guest"))
        gid = networks.network_content_id(guest_net)
        svc = _service([guest_net])
        self.assertTrue(networks.node_can_host_service(svc, lambda nid: nid == gid))
        self.assertFalse(networks.node_can_host_service(svc, lambda nid: nid != gid))

    def test_mixed_seed_and_guest(self):
        seed = _virtiofs_network(anchor=b"seed", handle="@seed")
        guest = _virtiofs_network(anchor=b"guest-net", tags=("shared-disk", "guest"), handle="@guest-net")
        gid = networks.network_content_id(guest)
        svc = _service([seed, guest])
        # Admissible only where the guest network already exists.
        self.assertTrue(networks.node_can_host_service(svc, lambda nid: nid == gid))
        self.assertFalse(networks.node_can_host_service(svc, lambda _nid: False))

    def test_declines_are_logged(self):
        svc = _service([_virtiofs_network(tags=("shared-disk", "guest"))])
        msgs = []
        networks.node_can_host_service(svc, lambda _nid: False, logger_fn=msgs.append)
        self.assertTrue(any("guest" in m.lower() for m in msgs))


if __name__ == "__main__":
    unittest.main()

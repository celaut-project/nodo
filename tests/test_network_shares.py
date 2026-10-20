"""Network shares (#268): a shared directory scoped by a Service.Network.

Two instances with no common parent share a directory when both declare the same
network and the same share. The network replaces the parent instance in the id,
the share has no owner, and a member runs on the node that already holds it.
"""
import importlib.util
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.utils import keyvalue
    from src.utils import shared_filesystems as sf
    from src.manager import shares as ms
    from src.manager import share_location as sl
    from src.gateway.launcher import launch_service as launch

    _VF_PATH = Path(__file__).resolve().parents[1] / "src" / "virtualizers" / "microvm" / "virtiofs.py"
    _spec = importlib.util.spec_from_file_location("virtiofs_network_under_test", _VF_PATH)
    vf = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(vf)
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = keyvalue = sf = ms = sl = launch = vf = None


def _dir(name, xattrs=None):
    b = celaut.Service.Container.Filesystem.ItemBranch(name=name)
    b.filesystem.SetInParent()
    for k, v in (xattrs or {}).items():
        keyvalue.set_value(b.xattrs, k, v)
    return b


def _service(branches, networks=()):
    s = celaut.Service()
    fs = celaut.Service.Container.Filesystem()
    for b in branches:
        fs.branch.append(b)
    s.container.filesystem = fs.SerializeToString()
    for tags, formal in networks:
        n = s.network.add()
        n.tags.extend(tags)
        if formal:
            n.formal = formal
    return s


def _member(path="data", tag=None, env=None, network="hdfs", networks=None, access=None):
    xattrs = {"share_network": network.encode()}
    if tag:
        xattrs["share_tag"] = tag.encode()
    if env:
        xattrs["share_env"] = env.encode()
    if access:
        xattrs["access"] = access.encode()
    if networks is None:
        networks = [(["hdfs"], b"cluster=${HDFS_CLUSTER}")]
    return _service([_dir(path, xattrs)], networks)


def _env(**values):
    return {k: v.encode() for k, v in values.items()}


def _config(**values):
    c = celaut.Configuration()
    for k, v in values.items():
        keyvalue.set_value(c.environment_variables, k, v.encode())
    return c


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DeclarationTests(unittest.TestCase):
    def test_share_network_is_a_declaration_of_its_own(self):
        [d] = sf.declarations_for_service(_member(tag="hdfs-data", env="HDFS_CLUSTER"))
        self.assertEqual(d.network, "hdfs")
        self.assertFalse(d.shared)
        self.assertFalse(d.guest)
        self.assertEqual(d.share_name, "hdfs-data")

    def test_it_cannot_be_combined_with_a_parent_share(self):
        for side in ("shared", "guest"):
            with self.subTest(side=side):
                service = _service(
                    [_dir("data", {"share_network": b"hdfs", side: b"true"})],
                    [(["hdfs"], b"")],
                )
                with self.assertRaisesRegex(ValueError, "symmetric"):
                    sf.declarations_for_service(service)

    def test_the_network_must_be_declared_by_the_service(self):
        service = _member(networks=[(["other"], b"")])
        with self.assertRaisesRegex(ValueError, "declares no network"):
            sf.declarations_for_service(service)

    def test_tag_alone_names_a_share_when_it_carries_share_network(self):
        # share_tag without shared/guest is a typo, but not with share_network.
        sf.declarations_for_service(_member(tag="hdfs-data"))

    def test_two_directories_cannot_share_one_name_in_one_network(self):
        service = _service(
            [
                _dir("a", {"share_network": b"hdfs", "share_tag": b"x"}),
                _dir("b", {"share_network": b"hdfs", "share_tag": b"x"}),
            ],
            [(["hdfs"], b"")],
        )
        with self.assertRaisesRegex(ValueError, "both shared in network 'hdfs'"):
            sf.declarations_for_service(service)

    def test_the_same_name_in_two_networks_is_two_shares(self):
        service = _service(
            [
                _dir("a", {"share_network": b"hdfs", "share_tag": b"x"}),
                _dir("b", {"share_network": b"other", "share_tag": b"x"}),
            ],
            [(["hdfs"], b""), (["other"], b"")],
        )
        self.assertEqual(len(sf.declarations_for_service(service)), 2)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class IdentityTests(unittest.TestCase):
    def _id(self, service, **env):
        [ref] = sf.network_refs(service, _env(**env))
        return ref.share_id

    def test_two_services_in_one_network_derive_one_id(self):
        # Different services, mounted at different paths: one share.
        a = _member(path="data", tag="hdfs-data", env="HDFS_CLUSTER")
        b = _member(path="mnt", tag="hdfs-data", env="HDFS_CLUSTER", access="ro")
        self.assertEqual(
            self._id(a, HDFS_CLUSTER="prod"), self._id(b, HDFS_CLUSTER="prod")
        )

    def test_the_substituted_formal_is_part_of_the_id(self):
        a = _member(tag="hdfs-data")
        self.assertNotEqual(
            self._id(a, HDFS_CLUSTER="prod"), self._id(a, HDFS_CLUSTER="test")
        )

    def test_a_formal_and_no_formal_are_two_shares(self):
        with_formal = _member(tag="hdfs-data", networks=[(["hdfs"], b"cluster=prod")])
        without = _member(tag="hdfs-data", networks=[(["hdfs"], b"")])
        self.assertNotEqual(self._id(with_formal), self._id(without))

    def test_the_named_tag_is_part_of_the_id(self):
        a = _member(tag="x", network="hdfs", networks=[(["hdfs", "alias"], b"")])
        b = _member(tag="x", network="alias", networks=[(["hdfs", "alias"], b"")])
        self.assertNotEqual(self._id(a), self._id(b))

    def test_a_network_id_never_equals_a_parent_id(self):
        # Domain-separated: whatever a parent id and a tag are called.
        self.assertNotEqual(
            sf.share_id("hdfs", "x"), sf.network_share_id("hdfs", b"", "x")
        )

    def test_an_unanswered_template_refuses_the_share(self):
        with self.assertRaises(sf.UnresolvedNetworkShare):
            sf.network_refs(_member(tag="x"), {})

    def test_the_ref_names_its_network(self):
        [ref] = sf.network_refs(_member(tag="x"), _env(HDFS_CLUSTER="prod"))
        self.assertEqual(ref.network, "hdfs")
        self.assertIn("network 'hdfs'", ref.describe())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AuthorizationTests(unittest.TestCase):
    def test_a_top_level_member_is_granted_without_any_exporter(self):
        with patch.object(ms.sc, "internal_instance_exists", return_value=False):
            refs = ms.authorize_network_shares(
                _member(tag="x"), "client-1", _config(HDFS_CLUSTER="prod")
            )
        self.assertEqual(len(refs), 1)

    def test_an_incomplete_formal_is_a_refusal(self):
        with self.assertRaisesRegex(ms.ShareAuthorizationError, "HDFS_CLUSTER"):
            ms.authorize_network_shares(_member(tag="x"), "client-1", _config())

    def test_an_ancestor_without_the_network_refuses_the_share(self):
        from src.manager import networks
        with patch.object(ms.sc, "internal_instance_exists", return_value=True), \
             patch.object(networks, "filter_networks_with_ancestors", return_value=[]):
            with self.assertRaisesRegex(ms.ShareAuthorizationError, "ancestor"):
                ms.authorize_network_shares(
                    _member(tag="x"), "vm-parent", _config(HDFS_CLUSTER="prod")
                )

    def test_an_ancestor_with_the_network_grants_it(self):
        from src.manager import networks
        with patch.object(ms.sc, "internal_instance_exists", return_value=True), \
             patch.object(
                 networks, "filter_networks_with_ancestors",
                 side_effect=lambda networks, father_id: networks,
             ):
            refs = ms.authorize_network_shares(
                _member(tag="x"), "vm-parent", _config(HDFS_CLUSTER="prod")
            )
        self.assertEqual(len(refs), 1)

    def test_no_network_share_costs_nothing(self):
        self.assertEqual(ms.authorize_network_shares(celaut.Service(), "c", None), [])


def _ref(share_id, path="/data", readonly=False):
    return sf.ShareRef(share_id=share_id, name="data", env="", discriminator="",
                       path=path, readonly=readonly, network="hdfs")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class BackendTests(unittest.TestCase):
    def _kwargs(self, base, sock, seeded):
        def fake_seed(mount, dest):
            seeded.append(mount.guest_path)
            (Path(dest) / "packaged.txt").write_text(mount.guest_path)

        return dict(
            base_dir=base, socket_dir=sock, virtiofsd_binary="virtiofsd",
            spawn_fn=lambda cmd, log_path: 1, pid_alive_fn=lambda pid: False,
            seed_fn=fake_seed,
        )

    def test_the_first_member_seeds_it_and_no_one_owns_it(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            sid = "1" * 64
            seeded = []
            kwargs = self._kwargs(base, sock, seeded)
            first = vf.mount_for(_ref(sid, "/data"), base, exported=False, network=True)
            second = vf.mount_for(_ref(sid, "/mnt"), base, exported=False, network=True)
            vf.ensure_share_backend(first, "vm-a", **kwargs)
            vf.ensure_share_backend(second, "vm-b", **kwargs)
            self.assertEqual(seeded, ["/data"])
            state = vf.load_share_state(base, sid)
            self.assertNotIn("owner", state)
            self.assertTrue(state["network"])
            self.assertTrue(vf.holds_network_share(base, sid))

    def test_it_ends_with_its_last_member_not_its_first(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            sid = "2" * 64
            kwargs = self._kwargs(base, sock, [])
            states = {}
            for vm in ("vm-a", "vm-b"):
                mount = vf.mount_for(_ref(sid), base, exported=False, network=True)
                states[vm] = [vf.ensure_share_backend(mount, vm, **kwargs)]
            kill = dict(base_dir=base, kill_fn=lambda pid: None)

            vf.teardown_virtiofs_for_vm("vm-a", states["vm-a"], **kill)
            self.assertTrue(vf.shared_dir(base, sid).is_dir())
            self.assertTrue(vf.holds_network_share(base, sid))

            vf.teardown_virtiofs_for_vm("vm-b", states["vm-b"], **kill)
            self.assertFalse(vf.share_state_dir(base, sid).exists())
            self.assertFalse(vf.holds_network_share(base, sid))

    def test_a_failed_seed_leaves_nothing_for_the_next_member(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            sid = "3" * 64
            mount = vf.mount_for(_ref(sid), base, exported=False, network=True)

            def broken_seed(mount, dest):
                (Path(dest) / "half").write_text("x")
                raise RuntimeError("debugfs failed")

            with self.assertRaises(RuntimeError):
                vf.ensure_share_backend(
                    mount, "vm-a", base_dir=base, socket_dir=sock,
                    virtiofsd_binary="virtiofsd", spawn_fn=lambda c, l: 1,
                    pid_alive_fn=lambda pid: False, seed_fn=broken_seed,
                )
            self.assertFalse(vf.share_state_dir(base, sid).exists())

    def test_a_parent_share_is_never_reported_as_held(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as sock:
            sid = "4" * 64
            mount = vf.mount_for(_ref(sid), base, exported=True)
            vf.ensure_share_backend(mount, "vm-parent", **self._kwargs(base, sock, []))
            self.assertFalse(vf.holds_network_share(base, sid))

    def test_a_read_only_image_mounts_every_share_read_only(self):
        for kwargs in ({"exported": True}, {"exported": False}, {"exported": False, "network": True}):
            mount = vf.mount_for(
                _ref("6" * 64, readonly=False), "/b", read_only_image=True, **kwargs
            )
            self.assertTrue(mount.readonly, kwargs)

    def test_a_writable_image_still_exports_read_write(self):
        mount = vf.mount_for(_ref("6" * 64, readonly=True), "/b", exported=True)
        self.assertFalse(mount.readonly)

    def test_a_member_keeps_the_mode_it_declared(self):
        mount = vf.mount_for(_ref("5" * 64, readonly=True), "/b", exported=False, network=True)
        self.assertTrue(mount.readonly)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PlacementTests(unittest.TestCase):
    A, B = _ref("a" * 64) if sf else None, _ref("b" * 64) if sf else None

    def _where(self, refs, local=(), peers=None):
        peers = peers or {}
        holders = sl.share_holders(
            refs, list(peers),
            holds_locally=lambda sid: sid in local,
            ask_peer=lambda peer, sid: sid in peers[peer],
        )
        return sl.placement(refs, holders)

    def test_a_share_nobody_holds_is_placed_freely(self):
        self.assertEqual(self._where([self.A], peers={"p1": set()}), [])

    def test_a_share_this_node_holds_runs_here(self):
        self.assertEqual(self._where([self.A], local={self.A.share_id}), [sl.LOCAL])

    def test_a_share_a_peer_holds_runs_on_that_peer(self):
        self.assertEqual(
            self._where([self.A], peers={"p1": set(), "p2": {self.A.share_id}}), ["p2"]
        )

    def test_two_shares_on_two_nodes_are_a_conflict(self):
        with self.assertRaisesRegex(sl.ShareLocationConflict, "different nodes"):
            self._where(
                [self.A, self.B],
                local={self.A.share_id},
                peers={"p1": {self.B.share_id}},
            )

    def test_one_share_on_two_nodes_offers_both_as_candidates(self):
        with patch.object(sl.log, "LOGGER") as logger:
            where = self._where(
                [self.A], local={self.A.share_id}, peers={"p1": {self.A.share_id}}
            )
        self.assertEqual(where, [sl.LOCAL, "p1"])
        self.assertIn("more than one node", logger.call_args.args[0])

    def test_a_split_share_is_narrowed_by_the_other_shares(self):
        where = self._where(
            [self.A, self.B],
            local={self.A.share_id},
            peers={"p1": {self.A.share_id, self.B.share_id}},
        )
        self.assertEqual(where, ["p1"])

    def test_a_peer_that_cannot_answer_is_taken_as_not_holding_it(self):
        holders = sl.share_holders(
            [self.A], ["p1"], holds_locally=lambda sid: False,
            ask_peer=lambda peer, sid: None,
        )
        self.assertEqual(sl.placement([self.A], holders), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LaunchPlacementTests(unittest.TestCase):
    def _launch(self, share_nodes, candidates):
        service = celaut.Service()
        cost = celaut.EstimatedCost(cost=launch.to_amount(1))
        patches = {
            "authorize_shares": None,
            "authorize_network_shares": [object()],
            "locate_network_shares": share_nodes,
            "declarations_for_service": [],
            "spend_mu": True,
            "enforce_network_policy": None,
            "evaluate_possible_environment_workloads": None,
            "_detect_local_preflight_failure": None,
            "descends_from_dev_client": True,
            "get_arch_tag": "amd64",
            "default_initial_balance": 1,
            # Pricing is not tested here: what funds a start has its own tests (#563).
            "_funding_failure": None,
            "_delegation_terms": (0, 0),
        }
        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(patch.object(launch, name, return_value=value))
            for target, name, value in (
                (launch.activity_window, "is_open", True),
                (launch.sc, "pop_forced_execution_peer", None),
                (launch.sc, "internal_instance_exists", False),
                (launch.demand_history, "record_admission", None),
                (launch.demand_history, "record_refusal", None),
            ):
                stack.enter_context(patch.object(target, name, return_value=value))
            balancer = stack.enter_context(patch.object(launch, "execution_balancer"))
            local = stack.enter_context(
                patch.object(launch, "local_execution", return_value="local-instance")
            )
            delegate = stack.enter_context(
                patch.object(launch, "delegate_execution", return_value="peer-instance")
            )
            balancer.return_value = iter([(peer, cost) for peer in candidates])
            try:
                result = launch.launch_service(
                    service=service, metadata=celaut.Metadata(),
                    configuration=celaut.Configuration(), father_id="client-1",
                    father_ip="10.0.0.1", service_id="svc",
                )
            except Exception as e:  # noqa: BLE001
                result = e
            return result, local, delegate

    def test_a_member_runs_on_the_peer_that_holds_the_share(self):
        result, local, delegate = self._launch(["p2"], ["local", "p1", "p2"])
        self.assertEqual(result, "peer-instance")
        local.assert_not_called()
        self.assertEqual(delegate.call_args.kwargs["peer"], "p2")

    def test_a_member_runs_here_when_this_node_holds_it(self):
        result, local, delegate = self._launch([sl.LOCAL], ["p1", "local"])
        self.assertEqual(result, "local-instance")
        delegate.assert_not_called()

    def test_a_share_nobody_holds_takes_the_cheapest_candidate(self):
        result, _local, delegate = self._launch([], ["p1", "local"])
        self.assertEqual(result, "peer-instance")
        self.assertEqual(delegate.call_args.kwargs["peer"], "p1")

    def test_a_split_share_lets_the_balancer_choose_between_its_holders(self):
        result, local, delegate = self._launch(["p1", "p2"], ["local", "p2", "p1"])
        self.assertEqual(result, "peer-instance")
        local.assert_not_called()
        self.assertEqual(delegate.call_args.kwargs["peer"], "p2")

    def test_the_holder_not_offered_by_the_balancer_fails_with_the_reason(self):
        result, local, delegate = self._launch(["p9"], ["local", "p1"])
        self.assertIsInstance(result, Exception)
        self.assertIn("held on p9", str(result))
        local.assert_not_called()
        delegate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

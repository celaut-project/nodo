"""Authorization of a shared filesystem: `guest` is an execution precondition.

A service that declares an inherited directory cannot run unless the instance
that launched it exports that share. The answer has to arrive before the balancer
and before any MU is spent, and a refusal has to say which of the two opposite
causes it is, because a hash can only ever say "not a member".
"""
import json
import unittest
from unittest.mock import patch

try:
    from protos import celaut_pb2 as celaut
    from src.manager import shares as ms
    from src.utils.registry_errors import ServiceNotInRegistry
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = ms = ServiceNotInRegistry = None


def _dir(name, xattrs=None, children=None):
    b = celaut.Service.Container.Filesystem.ItemBranch(name=name)
    b.filesystem.SetInParent()
    for c in (children or []):
        b.filesystem.branch.append(c)
    for k, v in (xattrs or {}).items():
        b.xattrs[k] = v
    return b


def _service(*branches):
    s = celaut.Service()
    fs = celaut.Service.Container.Filesystem()
    for b in branches:
        fs.branch.append(b)
    s.container.filesystem = fs.SerializeToString()
    return s


def _config(**envs):
    c = celaut.Configuration()
    for k, v in envs.items():
        c.environment_variables[k] = v.encode("utf-8")
    return c


TAG = {"share_tag": b"hdfs-data", "share_env": b"HDFS_CLUSTER"}
COORDINATOR = _service(_dir("data", dict(TAG, shared=b"true")))
DATANODE = _service(_dir("mnt", children=[_dir("hdfs", dict(TAG, guest=b"true"))]))


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class AuthorizeSharesTest(unittest.TestCase):
    def _authorize(self, service, father_id="vm-parent", config=None,
                   parent_spec=COORDINATOR, parent_envs=None, is_internal=True):
        """Authorize against a parent the node records as running ``parent_spec``."""
        envs = json.dumps(parent_envs) if parent_envs is not None else None
        with patch.object(ms.sc, "internal_instance_exists", return_value=is_internal), \
             patch.object(ms.sc, "get_service_id_by_container_id", return_value="svc-parent"), \
             patch.object(ms.sc, "get_local_instance_envs", return_value=envs), \
             patch.object(ms, "load_service_from_disk", return_value=parent_spec):
            return ms.authorize_shares(service=service, father_id=father_id, config=config)

    def test_a_service_without_guest_dirs_asks_nothing_of_anyone(self):
        # The ordinary case must not touch the database at all.
        plain = _service(_dir("mnt", children=[_dir("photos")]))
        with patch.object(ms.sc, "internal_instance_exists", side_effect=AssertionError):
            self.assertEqual(
                ms.authorize_shares(service=plain, father_id="vm-parent"), []
            )

    def test_the_matching_cluster_is_granted(self):
        granted = self._authorize(
            DATANODE, config=_config(HDFS_CLUSTER="prod"),
            parent_envs={"HDFS_CLUSTER": "prod"},
        )
        self.assertEqual([r.path for r in granted], ["/mnt/hdfs"])
        self.assertEqual(granted[0].name, "hdfs-data")

    def test_a_different_cluster_is_a_configuration_error(self):
        with self.assertRaises(ms.ShareAuthorizationError) as ctx:
            self._authorize(
                DATANODE, config=_config(HDFS_CLUSTER="test"),
                parent_envs={"HDFS_CLUSTER": "prod"},
            )
        message = str(ctx.exception)
        self.assertIn("configuration error", message)
        self.assertIn("HDFS_CLUSTER=prod", message)     # what the parent exports
        self.assertIn("HDFS_CLUSTER=test", message)     # what this service asked for

    def test_a_parent_that_exports_nothing_of_the_kind_is_a_composition_error(self):
        other = _service(_dir("other", {"shared": b"true", "share_tag": b"logs"}))
        with self.assertRaises(ms.ShareAuthorizationError) as ctx:
            self._authorize(
                DATANODE, config=_config(HDFS_CLUSTER="prod"), parent_spec=other,
            )
        message = str(ctx.exception)
        self.assertIn("composition error", message)
        self.assertIn("hdfs-data", message)   # what was asked for
        self.assertIn("logs", message)        # what is on offer

    def test_the_parent_launch_environment_is_what_counts(self):
        # The exporter resolved its share ids at boot from the values it was
        # launched with; anything current would be a different share.
        with self.assertRaises(ms.ShareAuthorizationError):
            self._authorize(
                DATANODE, config=_config(HDFS_CLUSTER="prod"), parent_envs={},
            )

    def test_a_top_level_launch_cannot_inherit_anything(self):
        # `nodo execute` draws a dev client id, and those come from a small
        # reusable pool: two unrelated runs must never reach one another's
        # directory. A client has no filesystem to export, so it is refused
        # rather than handed an empty one.
        with self.assertRaises(ms.ShareAuthorizationError) as ctx:
            self._authorize(DATANODE, father_id="dev-client-1", is_internal=False)
        self.assertIn("not an instance running on this node", str(ctx.exception))

    def test_no_parent_at_all_is_refused(self):
        with self.assertRaises(ms.ShareAuthorizationError):
            ms.authorize_shares(service=DATANODE, father_id="")

    def test_an_unreadable_parent_spec_denies_rather_than_grants_nothing(self):
        with patch.object(ms.sc, "internal_instance_exists", return_value=True), \
             patch.object(ms.sc, "get_service_id_by_container_id", return_value="svc"), \
             patch.object(ms, "load_service_from_disk",
                          side_effect=ServiceNotInRegistry("svc")):
            with self.assertRaises(ms.ShareAuthorizationError) as ctx:
                ms.authorize_shares(service=DATANODE, father_id="vm-parent")
        self.assertIn("inconsistent registry", str(ctx.exception))


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class RundevSharesTest(unittest.TestCase):
    """A rundev sandbox has no image and no spec, so it declares its exports."""

    def _sandbox(self, tmp, entries):
        (tmp / "__shares__").write_text(json.dumps(entries), encoding="utf-8")
        return f"rundev::{tmp}::4242"

    def test_a_declared_host_directory_is_granted_and_located(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            father = self._sandbox(tmp, [{
                "tag": "hdfs-data", "dir": str(tmp / "data"),
                "env": "HDFS_CLUSTER", "value": "prod",
            }])
            granted = ms.authorize_shares(
                service=DATANODE, father_id=father, config=_config(HDFS_CLUSTER="prod"),
            )
            self.assertEqual([r.name for r in granted], ["hdfs-data"])
            self.assertEqual(
                ms.rundev_host_dirs(father)[granted[0].share_id], str(tmp / "data")
            )

    def test_a_sandbox_that_declares_nothing_says_how_to_declare_it(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ms.ShareAuthorizationError) as ctx:
                ms.authorize_shares(service=DATANODE, father_id=f"rundev::{d}::1")
            self.assertIn("__shares__", str(ctx.exception))

    def test_no_host_directories_for_an_ordinary_parent(self):
        self.assertEqual(ms.rundev_host_dirs("vm-parent"), {})


if __name__ == "__main__":
    unittest.main()

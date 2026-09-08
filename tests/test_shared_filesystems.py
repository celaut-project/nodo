"""Unit tests for the parent -> child shared-filesystem xattr model."""
import unittest

try:
    from protos import celaut_pb2 as celaut
    from src.utils import shared_filesystems as sf
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = None
    sf = None


def _dir(name, xattrs=None, children=None):
    b = celaut.Service.Container.Filesystem.ItemBranch(name=name)
    b.filesystem.SetInParent()
    for c in (children or []):
        b.filesystem.branch.append(c)
    for k, v in (xattrs or {}).items():
        b.xattrs[k] = v
    return b


def _file(name, xattrs=None):
    b = celaut.Service.Container.Filesystem.ItemBranch(name=name, file=b"x")
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


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class SharedFilesystemsTest(unittest.TestCase):
    def test_no_declarations_for_plain_service(self):
        svc = _service(_dir("mnt", children=[_dir("photos")]))
        self.assertEqual(sf.declarations_for_service(svc), [])
        self.assertFalse(sf.service_requires_parent_colocation(svc))

    def test_exported_dir_defaults_to_rw(self):
        svc = _service(_dir("mnt", children=[_dir("photos", {"shared": b"true"})]))
        self.assertEqual(
            sf.exported_dirs(svc),
            [sf.SharedDir(path="/mnt/photos", shared=True, guest=False, access="rw")],
        )
        self.assertEqual(sf.guest_dirs(svc), [])

    def test_guest_dir_with_ro_access(self):
        svc = _service(_dir("data", {"guest": b"true", "access": b"ro"}))
        g = sf.guest_dirs(svc)
        self.assertEqual(len(g), 1)
        self.assertTrue(g[0].guest)
        self.assertTrue(g[0].readonly)
        self.assertTrue(sf.service_requires_parent_colocation(svc))

    def test_nested_paths_are_absolute(self):
        svc = _service(_dir("a", children=[_dir("b", children=[_dir("c", {"shared": b"true"})])]))
        self.assertEqual(sf.exported_dirs(svc)[0].path, "/a/b/c")

    def test_boolean_variants(self):
        for truthy in (b"true", b"1", b"TRUE", b"yes", b"on"):
            svc = _service(_dir("d", {"shared": truthy}))
            self.assertEqual(len(sf.exported_dirs(svc)), 1, truthy)
        for falsy in (b"false", b"0", b"no", b""):
            svc = _service(_dir("d", {"shared": falsy}))
            self.assertEqual(sf.exported_dirs(svc), [], falsy)

    def test_shared_and_guest_together_is_rejected(self):
        svc = _service(_dir("d", {"shared": b"true", "guest": b"true"}))
        with self.assertRaises(ValueError):
            sf.declarations_for_service(svc)

    def test_sharing_xattrs_on_file_is_rejected(self):
        svc = _service(_file("f", {"shared": b"true"}))
        with self.assertRaises(ValueError):
            sf.declarations_for_service(svc)

    def test_invalid_access_is_rejected(self):
        svc = _service(_dir("d", {"guest": b"true", "access": b"append"}))
        with self.assertRaises(ValueError):
            sf.declarations_for_service(svc)

    def test_traversing_share_path_is_rejected(self):
        # A Service arrives as a protobuf from another peer, so a branch name is
        # whatever the sender put in it. The node reads the exporter's subtree
        # off its image at this path to seed the share, so a path that is not its
        # own normal form is refused instead of resolved.
        for name in ("..", ".", ""):
            svc = _service(_dir("a", children=[_dir(name, {"shared": b"true"})]))
            with self.assertRaises(ValueError, msg=name):
                sf.declarations_for_service(svc)
        escaping = _service(_dir("a", children=[_dir("../../etc", {"guest": b"true"})]))
        with self.assertRaises(ValueError):
            sf.declarations_for_service(escaping)

    def test_share_id_is_stable_and_parent_scoped(self):
        a = sf.share_id("parent-A", "/mnt/photos")
        self.assertEqual(a, sf.share_id("parent-A", "/mnt/photos"))
        # different parent -> different share, even for the same path
        self.assertNotEqual(a, sf.share_id("parent-B", "/mnt/photos"))
        # different path -> different share
        self.assertNotEqual(a, sf.share_id("parent-A", "/mnt/other"))
        # different variable, and different value -> different share
        self.assertNotEqual(a, sf.share_id("parent-A", "/mnt/photos", "E"))
        self.assertNotEqual(
            sf.share_id("p", "n", "E", "a"), sf.share_id("p", "n", "E", "b")
        )

    def test_the_hashed_fields_cannot_be_confused_for_one_another(self):
        # Length-prefixed, not separator-joined: otherwise a child could reach a
        # share of its parent's it was never granted just by pushing the
        # separator into its own tag.
        self.assertNotEqual(
            sf.share_id("p", "a\x00b", "", "c"), sf.share_id("p", "a", "b", "c")
        )
        self.assertNotEqual(
            sf.share_id("p", "a", "", "b\x00c"), sf.share_id("p", "a\x00b", "", "c")
        )
        self.assertNotEqual(sf.share_id("ab", "c"), sf.share_id("a", "bc"))

    def test_share_id_requires_parent(self):
        with self.assertRaises(ValueError):
            sf.share_id("", "/mnt/photos")


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class ShareInvariantsTest(unittest.TestCase):
    """What a spec alone can be judged on, and therefore what pack time can."""

    def test_a_share_cannot_be_declared_inside_another(self):
        nested = _service(
            _dir("data", {"shared": b"true"}, children=[_dir("sub", {"shared": b"true"})])
        )
        with self.assertRaises(ValueError):
            sf.declarations_for_service(nested)

    def test_an_inherited_directory_cannot_be_re_exported(self):
        # The invariant: `shared` belongs to the instance whose own image holds
        # the directory. A `shared` inside a `guest` subtree would hand a
        # grandchild the grandparent's share.
        reexport = _service(
            _dir("data", {"guest": b"true"}, children=[_dir("sub", {"shared": b"true"})])
        )
        with self.assertRaises(ValueError):
            sf.declarations_for_service(reexport)

    def test_a_share_deeper_down_an_undeclared_tree_is_fine(self):
        ok = _service(_dir("mnt", children=[_dir("data", {"shared": b"true"})]))
        self.assertEqual([d.path for d in sf.exported_dirs(ok)], ["/mnt/data"])

    def test_two_directories_cannot_carry_the_same_name(self):
        # A path cannot repeat in a tree, so path-named shares never collide; a
        # tag can be repeated, and would resolve two directories to one share.
        for side in (b"shared", b"guest"):
            svc = _service(
                _dir("a", {side: b"true", "share_tag": b"x"}),
                _dir("b", {side: b"true", "share_tag": b"x"}),
            )
            with self.assertRaises(ValueError, msg=side):
                sf.declarations_for_service(svc)

    def test_one_tag_on_each_side_is_not_a_collision(self):
        # Export and import resolve against different parent ids, so the same
        # tag on both sides of one service is two different shares.
        svc = _service(
            _dir("a", {"shared": b"true", "share_tag": b"x"}),
            _dir("b", {"guest": b"true", "share_tag": b"x"}),
        )
        self.assertEqual(len(sf.declarations_for_service(svc)), 2)


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class ShareTagAndEnvTest(unittest.TestCase):
    """share_tag names the share instead of the path; share_env tells apart two
    shares of the same name. Both mirror Service.Network (tags / env var)."""

    def _decl(self, xattrs):
        return sf.declarations_for_service(_service(_dir("d", xattrs)))[0]

    def test_tag_and_env_are_parsed(self):
        d = self._decl({"shared": b"true", "share_tag": b"hdfs-data",
                        "share_env": b"HDFS_CLUSTER"})
        self.assertEqual((d.tag, d.env), ("hdfs-data", "HDFS_CLUSTER"))
        self.assertEqual(d.share_name, "hdfs-data")

    def test_share_name_falls_back_to_the_path(self):
        self.assertEqual(self._decl({"shared": b"true"}).share_name, "/d")

    def test_empty_tag_is_rejected(self):
        with self.assertRaises(ValueError):
            self._decl({"shared": b"true", "share_tag": b"  "})

    def test_tag_without_shared_or_guest_is_rejected(self):
        with self.assertRaises(ValueError):
            self._decl({"share_tag": b"hdfs-data"})

    def test_tag_makes_the_id_independent_of_the_mount_path(self):
        exporter = _dir("data", {"shared": b"true", "share_tag": b"hdfs-data"})
        importer = _dir("mnt", children=[
            _dir("hdfs", {"guest": b"true", "share_tag": b"hdfs-data"})
        ])
        [e] = sf.exported_dirs(_service(exporter))
        [i] = sf.guest_dirs(_service(importer))
        self.assertNotEqual(e.path, i.path)
        self.assertEqual(
            sf.share_ref("coordinator", e).share_id,
            sf.share_ref("coordinator", i).share_id,
        )

    def test_env_value_discriminates_two_clusters(self):
        [d] = sf.exported_dirs(
            _service(_dir("data", {"shared": b"true", "share_tag": b"hdfs-data",
                                   "share_env": b"HDFS_CLUSTER"}))
        )
        prod = sf.share_ref("p", d, {"HDFS_CLUSTER": b"prod"}).share_id
        test = sf.share_ref("p", d, {"HDFS_CLUSTER": b"test"}).share_id
        self.assertNotEqual(prod, test)
        self.assertEqual(prod, sf.share_ref("p", d, {"HDFS_CLUSTER": b"prod"}).share_id)
        # An undeclared value is the empty discriminator, shared by every
        # instance launched without the variable.
        self.assertEqual(
            sf.share_ref("p", d).share_id, sf.share_ref("p", d, {"OTHER": b"x"}).share_id
        )

    def test_both_sides_must_name_the_same_variable(self):
        # The parent declares share_env but was launched with no value for it;
        # the child declares no share_env at all. Collapsing both to an empty
        # discriminator would make them the same share, which is the
        # discriminator being skipped rather than matched -- so the variable's
        # *name* is part of the identity, and these two never meet.
        [exporter] = sf.exported_dirs(
            _service(_dir("data", {"shared": b"true", "share_env": b"E"}))
        )
        [importer] = sf.guest_dirs(_service(_dir("data", {"guest": b"true"})))
        self.assertNotEqual(
            sf.share_ref("p", exporter, {}).share_id,
            sf.share_ref("p", importer, {}).share_id,
        )
        # Declaring the same variable, both without a value, is a match: two
        # instances missing the same variable are in the same unnamed domain.
        [same] = sf.guest_dirs(
            _service(_dir("data", {"guest": b"true", "share_env": b"E"}))
        )
        self.assertEqual(
            sf.share_ref("p", exporter, {}).share_id,
            sf.share_ref("p", same, {}).share_id,
        )

    def test_a_share_ref_keeps_the_naming_parts_in_the_clear(self):
        # A hash cannot explain a refusal; these are what tell a composition
        # error from a configuration one.
        [d] = sf.exported_dirs(
            _service(_dir("data", {"shared": b"true", "share_tag": b"hdfs-data",
                                   "share_env": b"HDFS_CLUSTER"}))
        )
        ref = sf.share_ref("p", d, {"HDFS_CLUSTER": b"prod"})
        self.assertEqual((ref.name, ref.env, ref.discriminator), ("hdfs-data", "HDFS_CLUSTER", "prod"))
        self.assertIn("hdfs-data", ref.describe())
        self.assertIn("HDFS_CLUSTER=prod", ref.describe())

    def test_a_tag_is_matched_literally_and_its_charset_is_pinned(self):
        self.assertEqual(self._decl({"shared": b"true", "share_tag": b"  x-1 "}).tag, "x-1")
        for bad in (b"has space", b"tiene\nsalto", b"-leading", b"sla/sh"):
            with self.assertRaises(ValueError, msg=bad):
                self._decl({"shared": b"true", "share_tag": bad})
        for bad in (b"1BAD", b"has-dash", b"has space"):
            with self.assertRaises(ValueError, msg=bad):
                self._decl({"shared": b"true", "share_env": bad})
        # Case is significant: two tags differing only in case are two shares.
        self.assertNotEqual(sf.share_id("p", "Data"), sf.share_id("p", "data"))

    def test_env_without_tag_discriminates_the_path(self):
        [d] = sf.exported_dirs(
            _service(_dir("data", {"shared": b"true", "share_env": b"DATASET"}))
        )
        self.assertEqual(d.share_name, "/data")
        self.assertNotEqual(
            sf.share_ref("p", d, {"DATASET": b"a"}).share_id,
            sf.share_ref("p", d, {"DATASET": b"b"}).share_id,
        )


if __name__ == "__main__":
    unittest.main()

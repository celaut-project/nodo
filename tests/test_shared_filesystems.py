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

    def test_share_id_is_stable_and_parent_scoped(self):
        a = sf.share_id("parent-A", "/mnt/photos")
        self.assertEqual(a, sf.share_id("parent-A", "/mnt/photos"))
        # different parent -> different share, even for the same path
        self.assertNotEqual(a, sf.share_id("parent-B", "/mnt/photos"))
        # different path -> different share
        self.assertNotEqual(a, sf.share_id("parent-A", "/mnt/other"))

    def test_share_id_requires_parent(self):
        with self.assertRaises(ValueError):
            sf.share_id("", "/mnt/photos")


if __name__ == "__main__":
    unittest.main()

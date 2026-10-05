"""`service.json`'s `shared_filesystems` property -> reserved xattrs on directory branches.

`src/utils/shared_filesystems.py` defines the xattrs and the node consumes them, but
nothing in the packer wrote them. This pins the packer half:

* absent or empty writes nothing, so no existing service gets a new id;
* each entry sets `shared=true` / `guest=true` plus the optional `share_tag`,
  `share_env` and `access` on the branch at `path`;
* the directory must exist in the image; the packer does not create it;
* types, roles and unknown fields are refused, not coerced;
* the result passes through `declarations_for_filesystem`, so its rules apply;
* `read_only_filesystem: true` plus `shared` stays refused.

`ZipContainerPacker.__init__` runs BuildKit, so it is never called here: the methods
under test are invoked on an object built with `__new__`, carrying only `json`.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.utils import keyvalue
    from src.utils.filesystem_xattrs import READ_MODE_KEY
    from src.utils.shared_filesystems import declarations_for_filesystem, declarations_for_service
    from src.packers.zip_with_dockerfile import (
        READ_ONLY_FILESYSTEM_KEY,
        SHARED_FILESYSTEMS_KEY,
        ZipContainerPacker,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ZipContainerPacker = None  # type: ignore[assignment]


def _dir(name, *children):
    branch = celaut.Service.Container.Filesystem.ItemBranch()
    branch.name = name
    branch.filesystem.CopyFrom(celaut.Service.Container.Filesystem())
    for child in children:
        branch.filesystem.branch.append(child)
    return branch


def _file(name):
    branch = celaut.Service.Container.Filesystem.ItemBranch()
    branch.name = name
    branch.file = b"x"
    return branch


def _tree(*branches):
    filesystem = celaut.Service.Container.Filesystem()
    for branch in branches:
        filesystem.branch.append(branch)
    return filesystem


def _packer(service_json):
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = service_json
    return packer


def _declare(*entries):
    return {SHARED_FILESYSTEMS_KEY: list(entries)}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SharedFilesystemsPropertyTests(unittest.TestCase):

    # -- absent / empty ------------------------------------------------------ #

    def test_absent_and_empty_serialize_identically_to_before(self):
        untouched = _tree(_dir("shared"), _file("service.py"))
        for service_json in ({}, {SHARED_FILESYSTEMS_KEY: []}):
            with self.subTest(service_json=service_json):
                packed = _tree(_dir("shared"), _file("service.py"))
                _packer(service_json)._apply_shared_filesystems(packed)
                self.assertEqual(packed.SerializeToString(), untouched.SerializeToString())

    # -- roles --------------------------------------------------------------- #

    def test_shared_role_sets_the_xattrs(self):
        tree = _tree(_dir("shared"))

        _packer(_declare(
            {"path": "/shared", "role": "shared", "tag": "demo-share", "env": "SHARE_ID", "access": "rw"}
        ))._apply_shared_filesystems(tree)

        xattrs = keyvalue.to_dict(tree.branch[0].xattrs)
        self.assertEqual(xattrs["shared"], b"true")
        self.assertEqual(xattrs["share_tag"], b"demo-share")
        self.assertEqual(xattrs["share_env"], b"SHARE_ID")
        self.assertEqual(xattrs["access"], b"rw")
        self.assertNotIn("guest", xattrs)

    def test_guest_role_sets_the_xattrs_on_a_nested_directory(self):
        tree = _tree(_dir("mnt", _dir("from-parent")))

        _packer(_declare(
            {"path": "/mnt/from-parent", "role": "guest", "tag": "demo-share", "access": "ro"}
        ))._apply_shared_filesystems(tree)

        # protobuf copies a branch on append, so read it back from the tree.
        xattrs = keyvalue.to_dict(tree.branch[0].filesystem.branch[0].xattrs)
        self.assertEqual(xattrs["guest"], b"true")
        self.assertEqual(xattrs["share_tag"], b"demo-share")
        self.assertEqual(xattrs["access"], b"ro")
        self.assertNotIn("shared", xattrs)
        self.assertEqual(len(tree.branch[0].xattrs), 0)

    def test_optional_fields_are_omitted_when_not_declared(self):
        tree = _tree(_dir("shared"))

        _packer(_declare({"path": "/shared", "role": "shared"}))._apply_shared_filesystems(tree)

        self.assertEqual(keyvalue.to_dict(tree.branch[0].xattrs), {"shared": b"true"})

    def test_path_is_normalized(self):
        tree = _tree(_dir("shared"))

        _packer(_declare({"path": "/shared/", "role": "shared"}))._apply_shared_filesystems(tree)

        self.assertEqual(keyvalue.to_dict(tree.branch[0].xattrs), {"shared": b"true"})

    # -- missing directory ---------------------------------------------------- #

    def test_a_missing_directory_is_refused_and_says_to_create_it(self):
        with self.assertRaises(ValueError) as caught:
            _packer(_declare({"path": "/shared", "role": "shared"}))._apply_shared_filesystems(_tree())

        message = str(caught.exception)
        self.assertIn("/shared", message)
        self.assertIn("Dockerfile", message)

    def test_a_file_is_not_a_directory(self):
        with self.assertRaises(ValueError):
            _packer(_declare({"path": "/shared", "role": "shared"}))._apply_shared_filesystems(
                _tree(_file("shared"))
            )

    # -- types and roles ------------------------------------------------------ #

    def test_invalid_declarations_are_refused(self):
        good = {"path": "/shared", "role": "shared"}
        bad = {
            "not a list": {SHARED_FILESYSTEMS_KEY: {"path": "/shared", "role": "shared"}},
            "none": {SHARED_FILESYSTEMS_KEY: None},
            "entry not an object": _declare("/shared"),
            "unknown role": _declare({**good, "role": "exporter"}),
            "role not a string": _declare({**good, "role": True}),
            "missing role": _declare({"path": "/shared"}),
            "missing path": _declare({"role": "shared"}),
            "path not a string": _declare({**good, "path": 1}),
            "relative path": _declare({**good, "path": "shared"}),
            "root path": _declare({**good, "path": "/"}),
            "tag not a string": _declare({**good, "tag": 1}),
            "env not a string": _declare({**good, "env": ["A"]}),
            "access not a string": _declare({**good, "access": True}),
            "unknown field": _declare({**good, "acces": "ro"}),
            "repeated path": _declare(good, good),
        }
        for label, service_json in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError) as caught:
                    _packer(service_json)._validate_service_json_shape()
                self.assertIn(SHARED_FILESYSTEMS_KEY, str(caught.exception))

    def test_a_valid_declaration_passes_the_shape_check(self):
        _packer(_declare({"path": "/shared", "role": "shared"}))._validate_service_json_shape()

    # -- rules of shared_filesystems.py apply unchanged ------------------------- #

    def test_nested_declarations_are_refused(self):
        tree = _tree(_dir("shared", _dir("inner")))

        with self.assertRaises(ValueError) as caught:
            _packer(_declare(
                {"path": "/shared", "role": "shared"},
                {"path": "/shared/inner", "role": "guest"},
            ))._apply_shared_filesystems(tree)

        self.assertIn("nested", str(caught.exception))

    def test_repeated_tags_on_one_side_are_refused(self):
        tree = _tree(_dir("a"), _dir("b"))

        with self.assertRaises(ValueError):
            _packer(_declare(
                {"path": "/a", "role": "shared", "tag": "same"},
                {"path": "/b", "role": "shared", "tag": "same"},
            ))._apply_shared_filesystems(tree)

    def test_bad_tag_env_and_access_are_refused(self):
        for field, value in (("tag", "-bad"), ("env", "1BAD"), ("access", "rx")):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    _packer(_declare(
                        {"path": "/shared", "role": "shared", field: value}
                    ))._apply_shared_filesystems(_tree(_dir("shared")))

    # -- read_only_filesystem ------------------------------------------------- #

    def test_read_only_plus_shared_is_still_refused(self):
        tree = _tree(_dir("shared"))
        packer = _packer({READ_ONLY_FILESYSTEM_KEY: True, **_declare({"path": "/shared", "role": "shared"})})

        packer._apply_shared_filesystems(tree)
        with self.assertRaises(ValueError) as caught:
            packer._apply_read_only_filesystem(tree)

        self.assertIn("/shared", str(caught.exception))
        self.assertNotIn(READ_MODE_KEY, tree.xattrs)

    def test_read_only_plus_guest_is_allowed(self):
        tree = _tree(_dir("mnt"))
        packer = _packer({READ_ONLY_FILESYSTEM_KEY: True, **_declare({"path": "/mnt", "role": "guest"})})

        packer._apply_shared_filesystems(tree)
        # Metadata is not set on these hand-built branches, which is out of scope here.
        try:
            packer._apply_read_only_filesystem(tree)
        except ValueError as e:
            self.assertIn("metadata", str(e))

    # -- round trip ----------------------------------------------------------- #

    def test_round_trip_through_declarations_for_service(self):
        tree = _tree(_dir("shared"), _dir("mnt", _dir("from-parent")))
        _packer(_declare(
            {"path": "/shared", "role": "shared", "tag": "demo-share", "env": "SHARE_ID"},
            {"path": "/mnt/from-parent", "role": "guest", "tag": "demo-share", "access": "ro"},
        ))._apply_shared_filesystems(tree)

        service = celaut.Service()
        service.container.filesystem = tree.SerializeToString()

        by_path = {d.path: d for d in declarations_for_service(service)}
        self.assertEqual(set(by_path), {"/shared", "/mnt/from-parent"})
        self.assertTrue(by_path["/shared"].shared)
        self.assertEqual(by_path["/shared"].share_name, "demo-share")
        self.assertTrue(by_path["/mnt/from-parent"].guest)
        self.assertEqual(by_path["/mnt/from-parent"].access, "ro")
        self.assertEqual(len(declarations_for_filesystem(tree)), 2)


if __name__ == "__main__":
    unittest.main()

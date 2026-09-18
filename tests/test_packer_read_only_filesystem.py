"""`service.json`'s `read_only_filesystem` property -> `Filesystem.xattrs["read_mode"]`.

PR #374 wired the node side of `read_mode=ro` -- build, sizing, pricing, boot --
but left no way to *declare* it: the packer mapped `init.xattrs` and per-entry
`branch.xattrs` and nothing that reached the tree's own xattrs, so the only way to
produce a `ro` service was to hand-build the `Service` proto.

This pins the packer half of that contract:

* `read_only_filesystem: true` sets `read_mode=ro` on the **root** tree only.
  A nested `Filesystem` is a subdirectory, is not separately mounted, and a
  `read_mode` on one describes nothing (the proto comment from 84e9974c, and what
  `src/virtualizers/microvm/build.py` actually reads).
* Absent or `false` writes **nothing** -- not `read_mode=rw`. Absent already means
  `rw` to every reader, and the tree is hashed into the service id, so writing the
  default would change the id of every existing service on its next repack.
* Non-boolean is a packer error naming the field, refused in `__init__` before
  BuildKit runs, not coerced. `"true"` being truthy would make `"false"` truthy
  too, and build a service the opposite way round from the one declared.
* The two refusals the node would otherwise hit far later: a shared-filesystem
  export (mirroring `shares.py`), and incomplete per-entry metadata (mirroring
  `assert_complete_filesystem_metadata`).

`ZipContainerPacker.__init__` runs BuildKit, so it is never called here: the two
methods under test are invoked on an object built with `__new__`, with only the
`json` attribute they read. That is the whole of their input.
"""
import unittest

IMPORT_ERROR = None
try:
    # Before anything that builds a ConfigManager at import: the shipped example
    # points STORAGE at /nodo, which only exists on an installed node.
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.utils.filesystem_xattrs import (
        FILESYSTEM_METADATA_KEYS,
        READ_MODE_KEY,
        READ_MODE_RO,
        FilesystemNodeMetadata,
        encode_filesystem_metadata_xattrs,
        read_mode,
    )
    from src.packers.zip_with_dockerfile import (
        READ_ONLY_FILESYSTEM_KEY,
        ZipContainerPacker,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    ZipContainerPacker = None  # type: ignore[assignment]


def _metadata(mode=0o100644):
    return FilesystemNodeMetadata(
        mode=mode,
        uid=0,
        gid=0,
        mtime_ns=0,
        device_major=0,
        device_minor=0,
        device_is_block=False,
    )


def _branch(name, *, directory=False, xattrs=None, with_metadata=True):
    """One ItemBranch, with the full metadata contract unless told otherwise."""
    branch = celaut.Service.Container.Filesystem.ItemBranch()
    branch.name = name
    if with_metadata:
        encode_filesystem_metadata_xattrs(
            branch.xattrs, _metadata(0o40755 if directory else 0o100644)
        )
    for key, value in (xattrs or {}).items():
        branch.xattrs[key] = value
    if directory:
        branch.filesystem.CopyFrom(celaut.Service.Container.Filesystem())
    else:
        branch.file = b"x"
    return branch


def _tree(*branches):
    filesystem = celaut.Service.Container.Filesystem()
    for branch in branches:
        filesystem.branch.append(branch)
    return filesystem


def _packer(service_json):
    """A ZipContainerPacker carrying only the json its read_only path reads.

    __init__ drives BuildKit; these two methods read `self.json` and nothing else.
    """
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = service_json
    return packer


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReadOnlyFilesystemPropertyTests(unittest.TestCase):

    # -- true --------------------------------------------------------------- #

    def test_true_sets_read_mode_ro_on_the_root_tree(self):
        tree = _tree(_branch("bin", directory=True), _branch("service.py"))

        _packer({READ_ONLY_FILESYSTEM_KEY: True})._apply_read_only_filesystem(tree)

        self.assertEqual(tree.xattrs[READ_MODE_KEY], b"ro")
        # And it reads back through the helper the builder actually uses.
        self.assertEqual(read_mode(tree), READ_MODE_RO)

    def test_a_nested_filesystem_does_not_get_the_key(self):
        # A subdirectory is not separately mounted; build.py reads the root's
        # read_mode and ignores every other. Writing it deeper would be inert at
        # best and misleading at worst.
        inner_dir = _branch("deeper", directory=True)
        outer = _branch("bin", directory=True)
        outer.filesystem.branch.append(inner_dir)
        tree = _tree(outer)

        _packer({READ_ONLY_FILESYSTEM_KEY: True})._apply_read_only_filesystem(tree)

        self.assertEqual(tree.xattrs[READ_MODE_KEY], b"ro")
        self.assertNotIn(READ_MODE_KEY, outer.filesystem.xattrs)
        self.assertNotIn(READ_MODE_KEY, inner_dir.filesystem.xattrs)

    # -- absent / false ----------------------------------------------------- #

    def test_absent_writes_no_key_at_all(self):
        # Not read_mode=rw. Absent already means rw to every reader, and the tree
        # is hashed into the service id: writing the default would give every
        # existing service a new id on its next repack for no change in meaning.
        tree = _tree(_branch("service.py"))

        _packer({})._apply_read_only_filesystem(tree)

        self.assertNotIn(READ_MODE_KEY, tree.xattrs)
        self.assertEqual(len(tree.xattrs), 0)

    def test_false_writes_no_key_at_all(self):
        tree = _tree(_branch("service.py"))

        _packer({READ_ONLY_FILESYSTEM_KEY: False})._apply_read_only_filesystem(tree)

        self.assertNotIn(READ_MODE_KEY, tree.xattrs)
        self.assertEqual(len(tree.xattrs), 0)

    def test_an_absent_property_serializes_identically_to_before(self):
        # The hash-stability claim, made concrete: the bytes of a tree packed
        # without the property are exactly the bytes of one packed by a packer
        # that had never heard of it.
        untouched = _tree(_branch("bin", directory=True), _branch("service.py"))
        packed = _tree(_branch("bin", directory=True), _branch("service.py"))

        _packer({})._apply_read_only_filesystem(packed)

        self.assertEqual(packed.SerializeToString(), untouched.SerializeToString())

    # -- bad types ---------------------------------------------------------- #

    def test_the_string_true_is_an_error_not_a_yes(self):
        with self.assertRaises(ValueError) as caught:
            _packer({READ_ONLY_FILESYSTEM_KEY: "true"})._read_only_filesystem_requested()

        message = str(caught.exception)
        self.assertIn(READ_ONLY_FILESYSTEM_KEY, message)  # names the field
        self.assertIn("boolean", message)

    def test_other_non_booleans_are_errors(self):
        for value in ("false", "ro", 1, 0, [], {}, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    _packer(
                        {READ_ONLY_FILESYSTEM_KEY: value}
                    )._read_only_filesystem_requested()
                self.assertIn(READ_ONLY_FILESYSTEM_KEY, str(caught.exception))

    def test_a_bad_type_is_refused_before_the_build(self):
        # _validate_service_json_shape runs in __init__, before BuildKit is
        # invoked — the point being that a typo costs a syntax error, not a build.
        packer = _packer({READ_ONLY_FILESYSTEM_KEY: "true"})
        with self.assertRaises(ValueError):
            packer._validate_service_json_shape()

    def test_booleans_are_accepted(self):
        self.assertIs(
            _packer({READ_ONLY_FILESYSTEM_KEY: True})._read_only_filesystem_requested(),
            True,
        )
        self.assertIs(
            _packer({READ_ONLY_FILESYSTEM_KEY: False})._read_only_filesystem_requested(),
            False,
        )
        self.assertIs(_packer({})._read_only_filesystem_requested(), False)

    # -- refusal: shared-filesystem export ---------------------------------- #

    def test_true_plus_a_shared_export_is_refused(self):
        # Mirrors src/virtualizers/microvm/shares.py, which refuses this at launch:
        # a share is seeded from the exporter's image with `debugfs rdump`, an ext4
        # reader that cannot open a squashfs/erofs image. Refusing at pack time
        # means the operator finds out before publishing.
        tree = _tree(
            _branch("mnt", directory=True, xattrs={"shared": b"true"}),
        )

        with self.assertRaises(ValueError) as caught:
            _packer({READ_ONLY_FILESYSTEM_KEY: True})._apply_read_only_filesystem(tree)

        message = str(caught.exception)
        self.assertIn(READ_ONLY_FILESYSTEM_KEY, message)
        self.assertIn("/mnt", message)      # names the offending path
        self.assertIn("debugfs", message)   # and says why
        self.assertNotIn(READ_MODE_KEY, tree.xattrs)

    def test_a_nested_shared_export_is_also_caught(self):
        outer = _branch("srv", directory=True)
        outer.filesystem.branch.append(
            _branch("photos", directory=True, xattrs={"shared": b"true"})
        )
        tree = _tree(outer)

        with self.assertRaises(ValueError) as caught:
            _packer({READ_ONLY_FILESYSTEM_KEY: True})._apply_read_only_filesystem(tree)

        self.assertIn("/srv/photos", str(caught.exception))

    def test_an_inherited_guest_directory_is_allowed(self):
        # Only *exporting* is incompatible. A `guest` directory is mounted from the
        # parent's share; nothing is read out of this service's own image for it,
        # so it has no quarrel with an immutable rootfs.
        tree = _tree(_branch("mnt", directory=True, xattrs={"guest": b"true"}))

        _packer({READ_ONLY_FILESYSTEM_KEY: True})._apply_read_only_filesystem(tree)

        self.assertEqual(tree.xattrs[READ_MODE_KEY], b"ro")

    def test_a_shared_export_without_the_property_is_untouched(self):
        # The refusal is conditional on read_only_filesystem, not a new rule for
        # everyone: a plain rw service exporting a share still packs.
        tree = _tree(_branch("mnt", directory=True, xattrs={"shared": b"true"}))

        _packer({})._apply_read_only_filesystem(tree)

        self.assertNotIn(READ_MODE_KEY, tree.xattrs)

    # -- refusal: incomplete per-entry metadata ----------------------------- #

    def test_true_with_incomplete_per_entry_metadata_is_refused(self):
        # The packer emits all of FILESYSTEM_METADATA_KEYS on every branch it
        # builds, so this cannot happen today — the assertion is there so that if
        # that ever changes, the packer says so instead of shipping a service every
        # node will refuse at build time.
        tree = _tree(_branch("service.py", with_metadata=False))

        with self.assertRaises(ValueError) as caught:
            _packer({READ_ONLY_FILESYSTEM_KEY: True})._apply_read_only_filesystem(tree)

        message = str(caught.exception)
        self.assertIn(READ_ONLY_FILESYSTEM_KEY, message)
        self.assertIn("metadata", message)
        self.assertNotIn(READ_MODE_KEY, tree.xattrs)

    def test_metadata_incomplete_deeper_in_the_tree_is_refused(self):
        outer = _branch("bin", directory=True)
        outer.filesystem.branch.append(_branch("run.sh", with_metadata=False))
        tree = _tree(outer)

        with self.assertRaises(ValueError) as caught:
            _packer({READ_ONLY_FILESYSTEM_KEY: True})._apply_read_only_filesystem(tree)

        self.assertIn("run.sh", str(caught.exception))

    def test_incomplete_metadata_without_the_property_is_untouched(self):
        # rw keeps its legacy fallback; the gate is ro's cost, not a new rule.
        tree = _tree(_branch("service.py", with_metadata=False))

        _packer({})._apply_read_only_filesystem(tree)

        self.assertNotIn(READ_MODE_KEY, tree.xattrs)

    def test_the_packer_emits_every_metadata_key_on_every_branch(self):
        # The finding that makes the gate a no-op for this packer: the same call
        # recursive_parsing makes on each branch writes all seven keys at once.
        branch = _branch("service.py")
        for key in FILESYSTEM_METADATA_KEYS:
            self.assertIn(key, branch.xattrs)


if __name__ == "__main__":
    unittest.main()

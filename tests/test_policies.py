"""Policies (#670): a setting is identified by the exact bytes of its `formal`.

The scenarios of the specification `service/policies`: how a node identifies, validates
and refuses the settings of a service, the two settings defined now (`share`,
`read_mode`), and what the packer writes.
"""
import os
import unittest

IMPORT_ERROR = None
try:
    # The shipped example points STORAGE at /nodo, which only exists on an installed node.
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.utils import keyvalue, policies
    from src.utils import shared_filesystems as sf
    from src.utils.filesystem_xattrs import read_mode
    from src.packers.zip_with_dockerfile import ZipContainerPacker
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

Filesystem = celaut.Service.Container.Filesystem if IMPORT_ERROR is None else None
DOCS = os.path.join(os.path.dirname(__file__), "..", "docs", "policies")


def _policy(formal, value=b"", tags=()):
    return celaut.Policy(tags=list(tags), formal=formal, value=value)


def _dir(name, *policy_list, children=()):
    branch = Filesystem.ItemBranch(name=name)
    branch.filesystem.SetInParent()
    branch.policies.extend(policy_list)
    for child in children:
        branch.filesystem.branch.append(child)
    return branch


def _service(root_policies=(), init_policies=(), *branches):
    fs = Filesystem()
    fs.policies.extend(root_policies)
    for branch in branches:
        fs.branch.append(branch)
    service = celaut.Service()
    service.container.filesystem = fs.SerializeToString()
    service.container.init.policies.extend(init_policies)
    return service


def _read_mode(value):
    return policies.make(policies.READ_MODE, value)


def _share(value):
    return policies.make(policies.SHARE, value)


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class IdentifiedByFormalTests(unittest.TestCase):

    def test_same_tag_different_formal_is_a_setting_that_the_node_does_not_know(self):
        formal = bytearray(policies.READ_MODE_FORMAL)
        formal[-1] ^= 1  # one byte of difference
        fs = Filesystem()
        fs.policies.append(_policy(bytes(formal), b"ro", tags=["read_mode"]))
        with self.assertRaisesRegex(ValueError, "does not know"):
            read_mode(fs)

    def test_different_tags_same_formal_is_the_same_setting(self):
        fs = Filesystem()
        fs.policies.append(_policy(policies.READ_MODE_FORMAL, b"ro"))
        self.assertEqual(read_mode(fs), "ro")
        fs = Filesystem()
        fs.policies.append(_policy(policies.READ_MODE_FORMAL, b"ro", tags=["anything"]))
        self.assertEqual(read_mode(fs), "ro")

    def test_an_empty_formal_is_refused(self):
        service = _service([_policy(b"", b"ro", tags=["read_mode"])])
        with self.assertRaisesRegex(ValueError, "empty formal"):
            sf.declarations_for_service(service)

    def test_the_formal_bytes_are_the_ones_published_in_docs(self):
        for name, formal in (("share", policies.SHARE_FORMAL), ("read_mode", policies.READ_MODE_FORMAL)):
            with open(os.path.join(DOCS, f"{name}.formal"), "rb") as f:
                self.assertEqual(f.read(), formal, name)


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class RefusalTests(unittest.TestCase):

    def test_an_unknown_setting_is_refused_and_named(self):
        service = _service([_policy(b"setting=other", b"x", tags=["fancy"])])
        with self.assertRaises(ValueError) as caught:
            sf.declarations_for_service(service)
        message = str(caught.exception)
        self.assertIn("'fancy'", message)
        self.assertIn("root filesystem", message)
        self.assertIn("does not know", message)

    def test_a_policy_with_no_tags_is_named_as_such(self):
        service = _service([_policy(b"setting=other", b"x")])
        with self.assertRaisesRegex(ValueError, r"\(no tags\)"):
            sf.declarations_for_service(service)

    def test_a_known_setting_with_a_value_that_is_not_valid(self):
        service = _service([_read_mode(b"rx")])
        with self.assertRaises(ValueError) as caught:
            sf.declarations_for_service(service)
        self.assertIn("read_mode", str(caught.exception))
        self.assertIn("rx", str(caught.exception))
        with self.assertRaises(ValueError):
            read_mode(Filesystem(policies=[_read_mode(b"rx")]))

    def test_an_unknown_policy_on_a_directory_names_the_directory(self):
        service = _service((), (), _dir("data", _policy(b"setting=other", tags=["x"])))
        with self.assertRaisesRegex(ValueError, "directory '/data'"):
            sf.declarations_for_service(service)


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class ListTests(unittest.TestCase):

    def test_a_repeated_setting_is_refused_even_with_the_same_value(self):
        service = _service([_read_mode(b"ro"), _read_mode(b"ro")])
        with self.assertRaisesRegex(ValueError, "repeats"):
            sf.declarations_for_service(service)

    def test_the_packer_sorts_by_formal_and_the_reader_does_not_need_it(self):
        fs = Filesystem()
        policies.set_policy(fs.policies, _policy(b"zzz"))
        policies.set_policy(fs.policies, _policy(b"aaa"))
        policies.set_policy(fs.policies, _policy(b"mmm"))
        self.assertEqual([bytes(p.formal) for p in fs.policies], [b"aaa", b"mmm", b"zzz"])

    def test_a_list_that_is_not_sorted_is_still_read(self):
        # A sender sorts. A reader refuses repeats, not disorder: order has no meaning.
        unsorted = [_policy(b"zzz"), _policy(b"aaa")]
        with self.assertRaisesRegex(ValueError, "does not know"):
            policies.check_list(unsorted, policies.PLACE_ROOT, "x")
        self.assertEqual(policies.check_list([_read_mode(b"ro")], policies.PLACE_ROOT, "x"),
                         {"read_mode": "ro"})


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class PlaceTests(unittest.TestCase):

    def test_a_root_only_setting_on_a_subdirectory_is_refused(self):
        branch = _dir("sub")
        branch.filesystem.policies.append(_read_mode(b"ro"))
        with self.assertRaisesRegex(ValueError, "subdirectory '/sub'"):
            sf.declarations_for_service(_service((), (), branch))

    def test_a_root_only_setting_on_a_directory_entry_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not valid in this place"):
            sf.declarations_for_service(_service((), (), _dir("d", _read_mode(b"ro"))))

    def test_a_directory_setting_on_a_file_is_refused(self):
        branch = Filesystem.ItemBranch(name="f", file=b"x")
        branch.policies.append(_share(b"role=shared"))
        with self.assertRaisesRegex(ValueError, "not valid in this place"):
            sf.declarations_for_service(_service((), (), branch))

    def test_a_directory_setting_on_the_root_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not valid in this place"):
            sf.declarations_for_service(_service([_share(b"role=shared")]))

    def test_a_policy_on_init_is_refused(self):
        with self.assertRaises(ValueError):
            sf.declarations_for_service(_service((), [_read_mode(b"ro")]))


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class EntryXattrsTests(unittest.TestCase):

    def test_an_old_share_key_is_not_a_share_and_is_not_refused(self):
        branch = _dir("d")
        keyvalue.set_value(branch.xattrs, "shared", b"true")
        service = _service((), (), branch)
        self.assertEqual(sf.declarations_for_service(service), [])

    def test_an_unknown_entry_xattr_is_ignored(self):
        branch = _dir("d")
        keyvalue.set_value(branch.xattrs, "user.comment", b"x")
        self.assertEqual(sf.declarations_for_service(_service((), (), branch)), [])


def _old_form(key, value):
    """The wire of a `BytesKeyValue` entry in field 2, as old data had it."""
    entry = celaut.BytesKeyValue(key=key, value=value).SerializeToString()
    return b"\x12" + bytes([len(entry)]) + entry


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class OldFormIsRefusedTests(unittest.TestCase):

    def test_a_filesystem_packed_with_read_mode_as_an_xattr_is_refused(self):
        fs = Filesystem()
        fs.ParseFromString(_old_form("read_mode", b"ro"))
        with self.assertRaisesRegex(ValueError, "empty formal"):
            read_mode(fs)
        service = celaut.Service()
        service.container.filesystem = fs.SerializeToString()
        with self.assertRaisesRegex(ValueError, "empty formal"):
            sf.declarations_for_service(service)

    def test_a_service_packed_with_init_xattrs_is_refused(self):
        service = celaut.Service()
        service.container.init.ParseFromString(_old_form("some", b"key"))
        with self.assertRaisesRegex(ValueError, "empty formal"):
            sf.declarations_for_service(service)


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class ShareSettingTests(unittest.TestCase):

    def _declaration(self, value):
        [d] = sf.declarations_for_service(_service((), (), _dir("data", _share(value))))
        return d

    def test_exported_directory(self):
        d = self._declaration(b"role=shared")
        self.assertTrue(d.shared)
        self.assertEqual((d.access, d.share_name), ("rw", "/data"))

    def test_guest_with_a_tag_and_a_variable(self):
        d = self._declaration(b"access=ro\nenv=DATASET\nrole=guest\ntag=photos")
        self.assertTrue(d.guest and d.readonly)
        self.assertEqual((d.share_name, d.env), ("photos", "DATASET"))

    def test_unknown_key_in_the_value(self):
        with self.assertRaisesRegex(ValueError, "unknown key"):
            self._declaration(b"mode=1\nrole=shared")

    def test_network_key_without_network_role(self):
        with self.assertRaisesRegex(ValueError, "only valid with role=network"):
            self._declaration(b"network=lan\nrole=shared")

    def test_the_value_must_be_canonical(self):
        for bad in (b"tag=x\nrole=shared", b"role=shared\n", b"role=shared\nrole=guest",
                    b"", b"role=shared\n\ntag=x", b"role=shared\nrole"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self._declaration(bad)

    def test_role_is_required(self):
        with self.assertRaisesRegex(ValueError, "'role' is required"):
            self._declaration(b"tag=x")


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class ReadModeSettingTests(unittest.TestCase):

    def test_ro_and_rw(self):
        self.assertEqual(read_mode(Filesystem(policies=[_read_mode(b"ro")])), "ro")
        self.assertEqual(read_mode(Filesystem(policies=[_read_mode(b"rw")])), "rw")

    def test_no_policy_is_writable(self):
        self.assertEqual(read_mode(Filesystem()), "rw")


def _packer(service_json):
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = service_json
    return packer


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class PackerTests(unittest.TestCase):

    def test_init_xattrs_in_service_json_is_refused_and_says_it_was_removed(self):
        with self.assertRaisesRegex(ValueError, "'init.xattrs' was removed"):
            _packer({})._refuse_init_xattrs({"xattrs": {"k": "v"}})

    def test_no_init_xattrs_is_fine(self):
        _packer({})._refuse_init_xattrs({"entry_path": ["/bin/x"]})

    def test_a_service_without_settings_has_no_policy_bytes(self):
        # An empty repeated field is not serialized, so the id of a service that uses no
        # setting is the one it had before the field existed.
        self.assertEqual(Filesystem().SerializeToString(), b"")
        self.assertEqual(Filesystem.ItemBranch(name="d", file=b"x").SerializeToString(),
                         b"\n\x01d\x12\x01x")


if __name__ == "__main__":
    unittest.main()

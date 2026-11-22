"""The canonical encoding of a service (docs/CANONICAL_ENCODING.md).

The id of a service is the hash of its bytes, so these tests pin two things: the validator
accepts what protobuf writes for a message of the schema and refuses each other encoding of
the same value, and the node uses it where a service enters (the packer, ``StartService``,
``GetService``, ``nodo import``). The imports are not guarded: a test of the id that is
skipped says that the rule was checked when it was not.
"""
import os
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from bee_rpc import block_builder  # noqa: E402
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory  # noqa: E402
from google.protobuf.descriptor import FieldDescriptor as F  # noqa: E402

from protos import celaut_pb2 as celaut  # noqa: E402
# Imported here, not in a test: the first import of these modules loads a configuration, and
# a configuration loaded while the suite runs changes the identity of the node for the tests
# that follow (the TLS certificate is cached for the process).
import src.gateway.iterables.abstract_input_service_iterable as aisi  # noqa: E402
from src.packers import zip_with_dockerfile as packer_module  # noqa: E402
from src.utils import canonical_proto as cp  # noqa: E402
from src.utils import service_canonical  # noqa: E402
from src.utils.hashing import get_configured_hash_spec, hash_stream  # noqa: E402

ARCH = celaut.Service.Container.Architecture.DESCRIPTOR
SLOT = celaut.Service.Api.Slot.DESCRIPTOR
DEPENDENCY = celaut.Service.Workload.Dependency.DESCRIPTOR
SYSRESOURCES = celaut.Sysresources.DESCRIPTOR
BRANCH = celaut.Service.Container.Filesystem.ItemBranch.DESCRIPTOR
FILESYSTEM = celaut.Service.Container.Filesystem.DESCRIPTOR


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        low = n & 0x7F
        n >>= 7
        out.append(low | 0x80 if n else low)
        if not n:
            return bytes(out)


def field(number: int, klass: int, body: bytes = b"") -> bytes:
    return varint(number << 3 | klass) + body


def length(number: int, payload: bytes) -> bytes:
    return field(number, 2, varint(len(payload)) + payload)


def refuses(test, descriptor, data, *words, **kw):
    with test.assertRaises(cp.NotCanonical) as caught:
        cp.check(descriptor, data, **kw)
    for word in words:
        test.assertIn(word, str(caught.exception))


class WhatProtobufWritesIsCanonical(unittest.TestCase):
    def test_the_write_of_a_message_is_accepted(self):
        message = celaut.Service.Container.Architecture(tags=["linux/amd64", ""], prose="x")
        cp.check(ARCH, message.SerializeToString())

    def test_an_empty_message_is_accepted(self):
        cp.check(ARCH, b"")

    def test_a_message_that_is_set_and_empty_is_written(self):
        # A field with presence is written when it is set, also when it is empty.
        cp.check(celaut.Service.DESCRIPTOR, celaut.Service(container={}).SerializeToString())

    def test_an_optional_number_at_zero_is_written(self):
        message = celaut.Sysresources(mem_limit=0)
        self.assertTrue(message.SerializeToString())
        cp.check(SYSRESOURCES, message.SerializeToString())

    def test_an_empty_file_is_not_a_directory(self):
        empty_file = celaut.Service.Container.Filesystem.ItemBranch(name="a", file=b"")
        cp.check(BRANCH, empty_file.SerializeToString())
        self.assertIn(b"\x12\x00", empty_file.SerializeToString())

    def test_a_port_that_is_not_negative(self):
        cp.check(SLOT, celaut.Service.Api.Slot(port=80).SerializeToString())


class AnotherEncodingOfTheSameValueIsRefused(unittest.TestCase):
    """Each of these is read by protobuf as the same message, with other bytes."""

    CANONICAL = length(1, b"linux/amd64") + length(2, b"x")

    def test_the_canonical_bytes_are_what_protobuf_writes(self):
        self.assertEqual(
            celaut.Service.Container.Architecture(tags=["linux/amd64"], prose="x").SerializeToString(),
            self.CANONICAL)

    def test_the_fields_in_another_order(self):
        refuses(self, ARCH, length(2, b"x") + length(1, b"linux/amd64"), "comes after")

    def test_a_field_that_is_not_repeated_is_written_twice(self):
        refuses(self, ARCH, length(1, b"a") + length(2, b"y") + length(2, b"x"), "twice")

    def test_a_varint_with_extra_bytes(self):
        refuses(self, ARCH, length(1, b"a") + field(2, 2, b"\x81\x00x"), "extra bytes")
        refuses(self, ARCH, field(1, 2, b"\x81\x00"), "extra bytes")

    def test_a_value_at_its_default(self):
        refuses(self, ARCH, self.CANONICAL + length(3, b""), "default")
        refuses(self, ARCH, length(2, b""), "default")

    def test_a_number_at_its_default(self):
        refuses(self, DEPENDENCY, field(3, 0, b"\x00"), "default")

    def test_a_negative_number(self):
        negative = celaut.Service.Api.Slot(port=-1).SerializeToString()
        self.assertEqual(len(negative), 11)   # 10 bytes of varint
        refuses(self, SLOT, negative, "does not fit")

    def test_a_bool_that_is_not_0_or_1(self):
        refuses(self, DEPENDENCY, field(3, 0, b"\x02"), "does not fit")

    def test_a_class_that_is_not_used(self):
        for klass in (1, 3, 4, 5):
            with self.subTest(klass=klass):
                refuses(self, ARCH, field(2, klass, b"\x00" * 8), f"class {klass}")

    def test_a_number_with_the_class_of_a_length(self):
        refuses(self, SLOT, field(1, 2, b"\x01\x01"), "class of a length")

    def test_two_members_of_a_oneof(self):
        refuses(self, BRANCH, length(2, b"") + length(3, b""), "oneof item")

    def test_a_string_that_is_not_utf8(self):
        refuses(self, ARCH, length(2, b"\xff"), "UTF-8")

    def test_a_length_that_goes_past_the_message(self):
        refuses(self, ARCH, field(2, 2, varint(9) + b"x"), "past the message")

    def test_a_varint_that_is_cut(self):
        refuses(self, ARCH, b"\x12\x81", "cut")

    def test_a_varint_that_is_too_long(self):
        refuses(self, ARCH, b"\x80" * 11 + b"\x01", "more than 10 bytes")

    def test_the_field_number_zero(self):
        refuses(self, ARCH, field(0, 0, b"\x01"), "number 0")

    def test_messages_nested_too_deep(self):
        body = b""
        for _ in range(cp.MAX_DEPTH + 5):
            body = length(1, length(4, body))     # Filesystem.branch -> ItemBranch.filesystem
        refuses(self, FILESYSTEM, body, "deeper")


class FieldsThatTheSchemaDoesNotKnow(unittest.TestCase):
    KNOWN = length(1, b"linux/amd64")

    def test_they_are_skipped_by_default(self):
        cp.check(ARCH, self.KNOWN + field(50, 0, b"\x07") + length(51, b"new"))

    def test_they_are_refused_when_asked(self):
        refuses(self, ARCH, self.KNOWN + field(50, 0, b"\x07"), "not in the schema", unknown="refuse")

    def test_their_order_is_still_checked(self):
        refuses(self, ARCH, field(50, 0, b"\x07") + self.KNOWN, "comes after")

    def test_their_varints_and_classes_are_still_checked(self):
        refuses(self, ARCH, self.KNOWN + field(50, 0, b"\x81\x00"), "extra bytes")
        refuses(self, ARCH, self.KNOWN + field(50, 5, b"\x00" * 4), "class 5")
        refuses(self, ARCH, self.KNOWN + field(50, 2, varint(9) + b"x"), "past the message")


class TypesThatAreNotAllowed(unittest.TestCase):
    """The types of the schema that no message of an id has today."""

    @classmethod
    def setUpClass(cls):
        proto = descriptor_pb2.FileDescriptorProto(name="canonical_test.proto", package="t", syntax="proto3")
        message = proto.message_type.add(name="M")
        label = F.LABEL_OPTIONAL
        message.field.add(name="floats", number=1, type=F.TYPE_FLOAT, label=label)
        message.field.add(name="packed", number=2, type=F.TYPE_UINT32, label=F.LABEL_REPEATED)
        message.field.add(name="signed", number=3, type=F.TYPE_SINT32, label=label)
        entry = message.nested_type.add(name="PairsEntry")
        entry.options.map_entry = True
        entry.field.add(name="key", number=1, type=F.TYPE_STRING, label=label)
        entry.field.add(name="value", number=2, type=F.TYPE_STRING, label=label)
        message.field.add(name="pairs", number=4, type=F.TYPE_MESSAGE, label=F.LABEL_REPEATED,
                          type_name=".t.M.PairsEntry")
        pool = descriptor_pool.DescriptorPool()
        pool.Add(proto)
        cls.descriptor = pool.FindMessageTypeByName("t.M")
        cls.message = message_factory.GetMessageClass(cls.descriptor)

    def test_a_float_a_signed_number_and_a_map_are_refused(self):
        refuses(self, self.descriptor, field(1, 5, b"\x00" * 4), "class 5")
        refuses(self, self.descriptor, field(3, 0, b"\x02"), "type")
        refuses(self, self.descriptor, length(4, length(1, b"k") + length(2, b"v")), "map")

    def test_the_schema_says_which_fields_are_the_problem(self):
        found = cp.schema_violations(self.descriptor)
        self.assertEqual(len(found), 3, found)
        self.assertTrue(any("floats" in line for line in found))
        self.assertTrue(any("signed" in line for line in found))
        self.assertTrue(any("pairs" in line and "map" in line for line in found))

    def test_a_list_of_numbers_is_packed_and_not_empty(self):
        message = self.message()
        message.packed.extend([0, 1, 300])
        # Fields 1, 3 and 4 are not allowed: check the packed field alone.
        data = message.SerializeToString()
        cp.check(self.descriptor, data)
        refuses(self, self.descriptor, field(2, 0, b"\x01"), "not packed")
        refuses(self, self.descriptor, length(2, b""), "empty")
        refuses(self, self.descriptor, length(2, varint(2 ** 32)), "does not fit")


class TheSchemaOfAServiceUsesNothingThatIsNotAllowed(unittest.TestCase):
    """A type that is added to a message of an id changes what a reader must know. This
    fails first, so that it is a decision and not an accident."""

    def test_the_service(self):
        self.assertEqual(cp.schema_violations(celaut.Service.DESCRIPTOR), [])

    def test_the_filesystem(self):
        self.assertEqual(cp.schema_violations(FILESYSTEM), [])


def _random(descriptor, rng, depth=0):
    """A message of ``descriptor`` with random fields, built the way a packer builds one."""
    cls = message_factory.GetMessageClass(descriptor)
    message = cls()
    for f in descriptor.fields:
        if rng.random() < 0.25:
            continue
        for _ in range(rng.randint(0, 3) if f.label == F.LABEL_REPEATED else 1):
            if f.type == F.TYPE_MESSAGE:
                if depth > 3:
                    continue
                value = _random(f.message_type, rng, depth + 1)
                (getattr(message, f.name).add().CopyFrom(value) if f.label == F.LABEL_REPEATED
                 else getattr(message, f.name).CopyFrom(value))
                continue
            if f.type == F.TYPE_STRING:
                value = "".join(rng.choices("abc/é ", k=rng.randint(0, 6)))
            elif f.type == F.TYPE_BYTES:
                value = rng.randbytes(rng.randint(0, 6))
            elif f.type == F.TYPE_BOOL:
                value = rng.random() < 0.5
            elif f.type == F.TYPE_ENUM:
                value = rng.choice([v.number for v in f.enum_type.values])
            else:
                bits = {F.TYPE_INT32: 31, F.TYPE_UINT32: 32}.get(f.type, 64)
                value = rng.choice([0, 1, 80, 2 ** rng.randint(1, bits) - 1])
            (getattr(message, f.name).append(value) if f.label == F.LABEL_REPEATED
             else setattr(message, f.name, value))
    return message


class WhatTheSchemaOfAServiceWritesIsCanonical(unittest.TestCase):
    """Random messages of the whole schema, written with the normal call of protobuf (no
    ``deterministic``). Another implementation of protobuf is not tested here: this
    validator is what protects a node from it."""

    def _run(self, descriptor, count):
        rng = random.Random(11)
        for _ in range(count):
            message = _random(descriptor, rng)
            data = message.SerializeToString()
            try:
                cp.check(descriptor, data)
            except cp.NotCanonical as e:   # pragma: no cover - the failure shows the message
                self.fail(f"{e}\n{message}")

    def test_services(self):
        self._run(celaut.Service.DESCRIPTOR, 400)

    def test_filesystems(self):
        self._run(FILESYSTEM, 400)


# --------------------------- where the node uses it ---------------------------

def _setting(value):
    real = service_canonical.env_manager.get
    return patch.object(
        service_canonical.env_manager, "get",
        side_effect=lambda key, default=None: value if key == service_canonical.SETTING
        else real(key, default))


CANONICAL_SPEC = celaut.Service(prose="hello").SerializeToString()
# The same service with its fields in another order (protobuf reads it as the same message).
NOT_CANONICAL_SPEC = length(2, celaut.Service.Container(
    architecture=celaut.Service.Container.Architecture(tags=["linux/amd64"])).SerializeToString()) \
    + length(1, b"hello")


class ThePolicy(unittest.TestCase):
    def test_the_default_refuses(self):
        with _setting(None):
            self.assertEqual(service_canonical.policy(), "refuse")

    def test_a_value_that_is_not_one_of_the_three_refuses(self):
        for value in ("no", "false", "", 0, "refus"):
            with self.subTest(value=value), _setting(value):
                self.assertEqual(service_canonical.policy(), "refuse")

    def test_the_case_and_the_spaces_do_not_matter(self):
        with _setting(" Warn "):
            self.assertEqual(service_canonical.policy(), "warn")

    def test_refuse_warn_and_off(self):
        with _setting("refuse"), self.assertRaises(service_canonical.ServiceNotCanonical) as caught:
            service_canonical.enforce_spec(NOT_CANONICAL_SPEC)
        self.assertIn("docs/CANONICAL_ENCODING.md", str(caught.exception))
        self.assertIn("misc.CANONICAL_SERVICE", str(caught.exception))
        for value in ("warn", "off"):
            with self.subTest(value), _setting(value):
                service_canonical.enforce_spec(NOT_CANONICAL_SPEC)

    def test_a_canonical_specification_passes(self):
        with _setting("refuse"):
            service_canonical.enforce_spec(CANONICAL_SPEC)


class TheSpecificationAsStored(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _file(self, data: bytes) -> str:
        path = os.path.join(self.root, "service")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _multiblock(self, spec: celaut.Service) -> str:
        from bee_rpc.utils import modify_env
        modify_env(cache_dir=os.path.join(self.root, "cache") + "/",
                   block_dir=os.path.join(self.root, "blocks") + "/", block_depth=10)
        os.makedirs(os.path.join(self.root, "cache"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "blocks"), exist_ok=True)
        return block_builder.build_multiblock(pf_object_with_block_pointers=spec, blocks=[])[1]

    def test_a_file(self):
        with _setting("refuse"):
            service_canonical.enforce_spec_path(self._file(CANONICAL_SPEC))
            with self.assertRaises(service_canonical.ServiceNotCanonical):
                service_canonical.enforce_spec_path(self._file(NOT_CANONICAL_SPEC))

    def test_an_empty_specification_is_the_empty_message(self):
        with _setting("refuse"):
            service_canonical.enforce_spec_path(self._file(b""))

    def test_a_multiblock_directory_is_read_by_its_wbp_file(self):
        spec = celaut.Service(prose="hello")
        spec.container.architecture.tags.append("linux/amd64")
        spec.container.filesystem = b"\x0a\x03abc"      # what a pointer holds
        directory = self._multiblock(spec)
        self.assertTrue(os.path.isfile(os.path.join(directory, "wbp.bin")))
        with _setting("refuse"):
            service_canonical.enforce_spec_path(directory)

    def test_a_specification_that_cannot_be_read_is_refused_and_off_does_not_look(self):
        missing = os.path.join(self.root, "nothing")
        with _setting("refuse"), self.assertRaises(service_canonical.ServiceNotCanonical):
            service_canonical.enforce_spec_path(missing)
        with _setting("off"):
            service_canonical.enforce_spec_path(missing)

    def test_the_filesystem_block_that_the_packer_stored(self):
        filesystem = celaut.Service.Container.Filesystem()
        filesystem.branch.add(name="a", file=b"")
        filesystem.branch.add(name="b", file=b"data")
        directory = self._multiblock(filesystem)
        block_dir = os.path.join(self.root, "blocks")
        block_id = os.path.basename(directory.rstrip("/"))
        shutil.move(directory, os.path.join(block_dir, block_id))
        with _setting("refuse"):
            service_canonical.enforce_filesystem_block(block_id, block_dir)
            service_canonical.enforce_filesystem(filesystem.SerializeToString())
            with self.assertRaises(service_canonical.ServiceNotCanonical):
                service_canonical.enforce_filesystem(length(1, length(1, b"b")) + length(1, length(1, b"a")) + b"\x0a")


class TheIdOfWhatCame(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = os.path.join(self.root, "service")
        with open(self.path, "wb") as f:
            f.write(CANONICAL_SPEC)
        self.good = hash_stream([CANONICAL_SPEC], get_configured_hash_spec(service_canonical.env_manager)).hex()

    def test_the_hash_of_the_content_is_accepted(self):
        service_canonical.verify_received_id(self.path, self.good)

    def test_another_id_is_refused(self):
        with self.assertRaises(service_canonical.ServiceIdMismatch) as caught:
            service_canonical.verify_received_id(self.path, "00" * 32)
        self.assertIn(self.good, str(caught.exception))


class ThePackerChecksWhatItStores(unittest.TestCase):
    """``ZipContainerPacker.save``: the specification that it stored is the one that is
    checked. Only the block of the filesystem is replaced, because it needs a build."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        from bee_rpc.utils import modify_env
        for name in ("cache", "blocks"):
            os.makedirs(os.path.join(self.root, name))
        modify_env(cache_dir=os.path.join(self.root, "cache") + "/",
                   block_dir=os.path.join(self.root, "blocks") + "/", block_depth=10)
        self.built = []
        real = block_builder.build_multiblock

        def build(pf_object_with_block_pointers, blocks):
            result = real(pf_object_with_block_pointers=pf_object_with_block_pointers, blocks=[])
            self.built.append(result[1])
            if self.spoil:
                with open(os.path.join(result[1], "wbp.bin"), "wb") as f:
                    f.write(NOT_CANONICAL_SPEC)
            return result

        self.spoil = False
        patcher = patch.object(packer_module.block_builder, "build_multiblock", side_effect=build)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _packer(self):
        packer = object.__new__(packer_module.ZipContainerPacker)
        packer.service = packer_module.pack_pb2.Service(prose="hello")
        packer.filesystem_block = bytes(range(32))
        packer.metadata = celaut.Metadata()
        packer.tag = None
        return packer

    def test_a_canonical_specification_is_packed(self):
        with _setting("refuse"):
            service_id, _, directory = self._packer().save()
        self.assertTrue(service_id)
        self.assertTrue(os.path.isdir(directory))

    def test_a_specification_that_is_not_canonical_is_not_packed_and_does_not_stay(self):
        self.spoil = True
        with _setting("refuse"), self.assertRaises(service_canonical.ServiceNotCanonical):
            self._packer().save()
        self.assertFalse(os.path.exists(self.built[0]))

    def test_with_warn_it_is_packed(self):
        self.spoil = True
        with _setting("warn"):
            self._packer().save()


class StartServiceChecksWhatItStores(unittest.TestCase):
    """The ``Dir`` of a ``StartService`` request: the node stores it under the id that came,
    and it is what the node then runs and sends under that id."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.registry = os.path.join(self.root, "registry") + "/"
        os.makedirs(self.registry)
        for patcher in (patch.object(aisi, "REGISTRY", self.registry),
                        patch.object(aisi, "save_service", return_value=True)):
            self.saved = patcher.start()
            self.addCleanup(patcher.stop)
        self.save_service = aisi.save_service

    def _receive(self, data: bytes, claimed: str = None):
        path = os.path.join(self.root, "received")
        with open(path, "wb") as f:
            f.write(data)
        real = hash_stream([data], get_configured_hash_spec(service_canonical.env_manager)).hex()
        iterable = aisi.AbstractInputServiceIterable(iter(()), object())
        iterable.service_hash = claimed or real
        iterable.generated = True          # nothing after the save
        list(iterable._AbstractInputServiceIterable__pattern_matching(
            aisi.Dir(dir=path, _type=celaut.Service)))
        return iterable, path

    def test_a_canonical_service_that_hashes_to_its_id_is_stored(self):
        with _setting("refuse"):
            iterable, _ = self._receive(CANONICAL_SPEC)
        self.assertTrue(iterable.service_saved)
        self.save_service.assert_called_once()

    def test_another_content_under_the_id_is_not_stored(self):
        with _setting("refuse"), self.assertRaises(service_canonical.ServiceIdMismatch):
            self._receive(CANONICAL_SPEC, claimed="ab" * 32)
        self.save_service.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.root, "received")))

    def test_that_check_does_not_depend_on_the_setting(self):
        with _setting("off"), self.assertRaises(service_canonical.ServiceIdMismatch):
            self._receive(CANONICAL_SPEC, claimed="ab" * 32)
        self.save_service.assert_not_called()

    def test_a_service_that_is_not_canonical_is_not_stored(self):
        with _setting("refuse"), self.assertRaises(service_canonical.ServiceNotCanonical):
            self._receive(NOT_CANONICAL_SPEC)
        self.save_service.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.root, "received")))

    def test_with_warn_it_is_stored(self):
        with _setting("warn"):
            iterable, _ = self._receive(NOT_CANONICAL_SPEC)
        self.assertTrue(iterable.service_saved)

    def test_a_service_that_the_node_holds_is_not_read_again(self):
        data = CANONICAL_SPEC
        claimed = hash_stream([data], get_configured_hash_spec(service_canonical.env_manager)).hex()
        os.makedirs(os.path.join(self.registry, claimed))
        with _setting("refuse"):
            self._receive(NOT_CANONICAL_SPEC, claimed=claimed)    # not stored again, no check
        self.save_service.assert_called_once()


if __name__ == "__main__":
    unittest.main()

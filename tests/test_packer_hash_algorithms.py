"""The packer must compute every digest it records, not borrow bee-rpc's.

`build_multiblock` returns an id that is always sha3_256: the library has no
notion of the `hashing.HASH` this node is configured with. `save()` used to take
that id and file it under SHA3_256_ID whenever the configured algorithm was
something else -- right only for as long as the library kept hashing the way the
node assumed, and silently mislabelled the moment it did not.

Every digest is now taken from the service's expanded content, so what the
metadata claims is what the content hashes to under that algorithm. That
includes two mandatory companions -- SHA3_256 and BLAKE2B -- alongside
whichever algorithm `hashing.HASH` configures: any node, regardless of its own
configured algorithm, must be able to resolve a service it did not pack itself
(see `get_service_hex_main_hash`'s SHA3_256 fallback), and a service should stay
resolvable if a node's `hashing.HASH` is ever switched to BLAKE2B, without a
repack.
"""
import hashlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

from bee_rpc import block_builder
from bee_rpc.reader import read_multiblock_directory
from bee_rpc.utils import modify_env

import src.packers.zip_with_dockerfile as packer
from src.packers.zip_with_dockerfile import ZipContainerPacker
from protos import pack_pb2

from protos import celaut_pb2 as celaut
from src.utils.hashing import (
    BLAKE2B_ID,
    HASH_SPECS,
    SHA256_ID,
    SHA3_256_ID,
    SHAKE_256_ID,
    hash_stream,
    hash_stream_many,
)


class HashStreamManyTests(unittest.TestCase):
    def test_one_pass_agrees_with_hashing_each_separately(self):
        chunks = [b"a" * 1000, b"b" * 37, b"", b"c" * 4096]
        specs = [HASH_SPECS[i] for i in
                 (SHA256_ID, SHA3_256_ID, SHAKE_256_ID, BLAKE2B_ID)]

        together = hash_stream_many(iter(chunks), specs)
        for spec in specs:
            self.assertEqual(
                together[spec.id_bytes], hash_stream(iter(chunks), spec),
                f"{spec.name} differs when hashed alongside the others")

    def test_the_digests_are_of_the_whole_stream(self):
        chunks = [b"one", b"two", b"three"]
        together = hash_stream_many(iter(chunks), [HASH_SPECS[SHA3_256_ID]])
        self.assertEqual(together[SHA3_256_ID],
                         hashlib.sha3_256(b"".join(chunks)).digest())

    def test_an_empty_stream_still_yields_the_empty_digests(self):
        together = hash_stream_many(iter([]), [HASH_SPECS[SHA256_ID]])
        self.assertEqual(together[SHA256_ID], hashlib.sha256(b"").digest())


class PackerRecordsRealDigestsTests(unittest.TestCase):
    """What `save()` actually writes into the metadata, per configured algorithm.

    The build step is skipped -- the filesystem block is made here instead of by
    BuildKit -- but `save()` itself runs, so the rule under test is the code's,
    not a copy of it: every hash it records is the service's content hashed
    under the type it is filed as, including the two mandatory companions
    (SHA3_256, BLAKE2B) a differently-configured node also stores. SHA3_256 is
    the one that used to be taken from build_multiblock's return value.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="packer-hash-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.blocks = os.path.join(self.root, "blocks")
        os.makedirs(self.blocks)
        modify_env(cache_dir=self.root + os.sep, block_dir=self.blocks + os.sep)
        self.addCleanup(modify_env, cache_dir=packer.CACHE, block_dir=packer.BLOCKDIR)
        self._blockdir = packer.BLOCKDIR
        packer.BLOCKDIR = self.blocks + os.sep
        self.addCleanup(setattr, packer, "BLOCKDIR", self._blockdir)

    def _prepared(self):
        """A packer with everything save() needs, short of running it."""
        filesystem = celaut.Service.Container.Filesystem()
        for name in ("app.py", "README"):
            branch = filesystem.branch.add()
            branch.name = name
            branch.file = name.encode() * 64

        spec = ZipContainerPacker.__new__(ZipContainerPacker)
        spec.blocks = []
        spec.metadata = celaut.Metadata()
        spec.tag = "demo"
        spec.service = pack_pb2.Service()
        spec.service.container.init.entry_path.append("app.py")
        spec.filesystem_block = packer._install_as_block(
            *block_builder.build_multiblock(filesystem, [])
        )
        return spec

    @staticmethod
    def _content(directory):
        return b"".join(read_multiblock_directory(directory=directory, ignore_blocks=True))

    def _packed(self, configured_id):
        """Run save() with `hashing.HASH` set to one algorithm."""
        spec = self._prepared()
        with mock.patch.object(packer, "get_configured_hash_spec",
                               return_value=HASH_SPECS[configured_id]):
            service_id, metadata, directory = spec.save()
        return service_id, metadata, self._content(directory)

    def test_the_recorded_sha3_does_not_come_from_the_library(self):
        """bee-rpc's object id happens to be sha3 today; nodo must not rely on it.

        With the library returning something else, a node that filed that value
        as its SHA3_256 entry would publish a hash of nothing. The entry has to
        be the node's own hash of the content.
        """
        spec = self._prepared()
        honest = packer.block_builder.build_multiblock

        def lying(**kwargs):
            _id, directory = honest(**kwargs)
            return b"\x00" * 32, directory

        with mock.patch.object(packer.block_builder, "build_multiblock",
                               side_effect=lying), \
                mock.patch.object(packer, "get_configured_hash_spec",
                                  return_value=HASH_SPECS[SHA256_ID]):
            service_id, metadata, directory = spec.save()

        content = self._content(directory)
        companion = [h for h in metadata.hashtag.hash if h.type == SHA3_256_ID]
        self.assertEqual(len(companion), 1)
        self.assertNotEqual(companion[0].value, b"\x00" * 32,
                            "the SHA3 entry was taken from the library's id")
        self.assertEqual(companion[0].value, hashlib.sha3_256(content).digest())
        self.assertEqual(service_id,
                         hash_stream(iter([content]), HASH_SPECS[SHA256_ID]).hex())

    def test_every_recorded_hash_matches_its_declared_algorithm(self):
        for configured_id in (SHA256_ID, SHA3_256_ID, SHAKE_256_ID, BLAKE2B_ID):
            with self.subTest(algorithm=HASH_SPECS[configured_id].name):
                service_id, metadata, content = self._packed(configured_id)
                self.assertTrue(metadata.hashtag.hash)
                for entry in metadata.hashtag.hash:
                    entry_spec = HASH_SPECS[entry.type]
                    self.assertEqual(
                        entry.value, hash_stream(iter([content]), entry_spec),
                        f"entry filed as {entry_spec.name} is not that hash of the content")

    def test_the_service_id_is_the_configured_hash_of_the_content(self):
        for configured_id in (SHA256_ID, SHA3_256_ID, SHAKE_256_ID, BLAKE2B_ID):
            with self.subTest(algorithm=HASH_SPECS[configured_id].name):
                service_id, metadata, content = self._packed(configured_id)
                self.assertEqual(
                    service_id,
                    hash_stream(iter([content]), HASH_SPECS[configured_id]).hex())

    def test_a_non_sha3_node_also_records_a_real_sha3(self):
        # SHA3_256 is a mandatory companion for every configured algorithm
        # except itself -- it is bee-rpc's own block-addressing hash and
        # hashing.py's DEFAULT_HASH_NAME, so any node can resolve a service by
        # it regardless of what that node's own hashing.HASH is set to.
        for configured_id in (SHA256_ID, SHAKE_256_ID, BLAKE2B_ID):
            with self.subTest(algorithm=HASH_SPECS[configured_id].name):
                _, metadata, content = self._packed(configured_id)
                companion = [h for h in metadata.hashtag.hash if h.type == SHA3_256_ID]
                self.assertEqual(len(companion), 1)
                self.assertEqual(companion[0].value, hashlib.sha3_256(content).digest())

    def test_a_non_blake2b_node_also_records_a_real_blake2b(self):
        # The second mandatory companion: a service stays resolvable if some
        # node's hashing.HASH is ever switched to BLAKE2B, without a repack.
        for configured_id in (SHA256_ID, SHA3_256_ID, SHAKE_256_ID):
            with self.subTest(algorithm=HASH_SPECS[configured_id].name):
                _, metadata, content = self._packed(configured_id)
                companion = [h for h in metadata.hashtag.hash if h.type == BLAKE2B_ID]
                self.assertEqual(len(companion), 1)
                self.assertEqual(companion[0].value,
                                 hashlib.blake2b(content, digest_size=32).digest())

    def test_a_companion_algorithm_is_never_recorded_twice(self):
        # SHA3_256 and BLAKE2B are each also a valid *configured* choice, and
        # being both the primary entry and a companion must collapse to one.
        for configured_id in (SHA3_256_ID, BLAKE2B_ID):
            with self.subTest(algorithm=HASH_SPECS[configured_id].name):
                _, metadata, _content = self._packed(configured_id)
                types = [h.type for h in metadata.hashtag.hash]
                self.assertEqual(len(types), len(set(types)))
                self.assertIn(configured_id, types)

    def test_every_algorithm_ends_up_with_both_companions_present(self):
        for configured_id in (SHA256_ID, SHA3_256_ID, SHAKE_256_ID, BLAKE2B_ID):
            with self.subTest(algorithm=HASH_SPECS[configured_id].name):
                _, metadata, _content = self._packed(configured_id)
                types = {h.type for h in metadata.hashtag.hash}
                self.assertEqual(types, {configured_id, SHA3_256_ID, BLAKE2B_ID})


if __name__ == "__main__":
    unittest.main()

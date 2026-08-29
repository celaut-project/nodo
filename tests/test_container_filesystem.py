"""Reading a container filesystem that is stored as a block.

The tree a build consumes must keep the per-file block pointers in it. Expanding
the filesystem block instead substitutes every sub-block's content back inline,
which pulls the large files into memory -- undoing the reason they were stored
out of line, and leaving `ch/build.py`'s `_write_item` with nothing to stream.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from bee_rpc import block_builder, buffer_pb2
from bee_rpc.client import copy_block_if_exists
from bee_rpc.utils import Enviroment, modify_env

import src.packers.zip_with_dockerfile as packer
from protos import celaut_pb2 as celaut
from bee_rpc.utils import block_pointer, hash_types_for_packing
from src.utils.container_filesystem import (
    filesystem_block_id,
    filesystem_hash_types,
    load_container_filesystem,
)

BIG = b"L" * 40_000          # over the threshold used below -> stored as a block
SMALL = b"s" * 300           # under it -> inlined


class ContainerFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cfs-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.blocks = os.path.join(self.root, "blocks")
        os.makedirs(self.blocks)
        modify_env(cache_dir=self.root + os.sep, block_dir=self.blocks + os.sep)
        self.addCleanup(modify_env, cache_dir=packer.CACHE, block_dir=packer.BLOCKDIR)
        self._blockdir, packer.BLOCKDIR = packer.BLOCKDIR, self.blocks + os.sep
        self.addCleanup(setattr, packer, "BLOCKDIR", self._blockdir)

    def _filesystem(self):
        """A tree with one inlined file and one stored as a block."""
        big_path = os.path.join(self.root, "big.bin")
        with open(big_path, "wb") as f:
            f.write(BIG)
        block_hash, block = block_builder.create_block(file_path=big_path, copy=True)

        filesystem = celaut.Service.Container.Filesystem()
        small = filesystem.branch.add()
        small.name = "small.txt"
        small.file = SMALL
        big = filesystem.branch.add()
        big.name = "big.bin"
        big.file = block.SerializeToString()
        return filesystem, [block_hash]

    def _service_with_filesystem_block(self):
        filesystem, blocks = self._filesystem()
        block_id = packer._install_as_block(
            *block_builder.build_multiblock(filesystem, blocks))
        service = celaut.Service()
        service.container.filesystem = buffer_pb2.Buffer.Block(
            hashes=[buffer_pb2.Buffer.Block.Hash(
                type=Enviroment.hash_type, value=block_id)]
        ).SerializeToString()
        return service

    def test_an_inline_filesystem_is_read_as_is(self):
        filesystem, _ = self._filesystem()
        service = celaut.Service()
        service.container.filesystem = filesystem.SerializeToString()

        self.assertIsNone(filesystem_block_id(service.container.filesystem))
        loaded = load_container_filesystem(service)
        self.assertEqual([b.name for b in loaded.branch], ["small.txt", "big.bin"])

    def test_a_filesystem_block_is_recognised_as_one(self):
        service = self._service_with_filesystem_block()
        self.assertIsNotNone(filesystem_block_id(service.container.filesystem))

    def test_the_large_file_stays_a_pointer(self):
        # The regression this guards: expanding the block would put all 40 kB
        # back into the message, and on a real image that is the whole rootfs.
        loaded = load_container_filesystem(self._service_with_filesystem_block())
        big = {b.name: b for b in loaded.branch}["big.bin"]
        self.assertLess(len(big.file), len(BIG) // 100,
                        "the block was expanded back into the message")
        # Two pointer encodings exist -- create_block's carries the hash type,
        # the block driver's omits it -- so what matters is that it is a Block
        # naming a stored block, not its exact size.
        self.assertIsNotNone(filesystem_block_id(big.file))

    def test_the_small_file_is_still_there_inline(self):
        loaded = load_container_filesystem(self._service_with_filesystem_block())
        small = {b.name: b for b in loaded.branch}["small.txt"]
        self.assertEqual(small.file, SMALL)

    def test_the_pointer_still_resolves_to_the_original_content(self):
        # What ch/build.py's _write_item does with it: stream the block into place.
        loaded = load_container_filesystem(self._service_with_filesystem_block())
        big = {b.name: b for b in loaded.branch}["big.bin"]
        target = os.path.join(self.root, "restored.bin")
        self.assertTrue(copy_block_if_exists(buffer=big.file, directory=target))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), BIG)

    def test_a_large_inline_filesystem_is_never_taken_for_a_pointer(self):
        # And is not handed to ParseFromString to find that out: an inline
        # filesystem is the whole image, and this runs twice per launch.
        filesystem = celaut.Service.Container.Filesystem()
        branch = filesystem.branch.add()
        branch.name = "big"
        branch.file = b"x" * 100_000
        raw = filesystem.SerializeToString()
        self.assertGreater(len(raw), 512)
        self.assertIsNone(filesystem_block_id(raw))

        service = celaut.Service()
        service.container.filesystem = raw
        self.assertEqual(
            [b.name for b in load_container_filesystem(service).branch], ["big"])

    def test_a_service_with_no_filesystem_reads_as_empty(self):
        self.assertEqual(len(load_container_filesystem(celaut.Service()).branch), 0)


class CompressedPointersInTheFilesystemBlock(unittest.TestCase):
    """The per-file pointers inside a filesystem block leave their hash types out.

    The filesystem is stored as a block of its own, so those pointers sit one
    level down and take their types from the pointer that names it. An image of a
    few thousand large files would otherwise repeat the same 32 bytes a few
    thousand times to say what the level above already said. The pointer in the
    spec -- the top of what reaches disk -- still states it: there is nothing
    above it, and it is what another node has to read without sharing this one's
    configuration.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cfs-compressed-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.blocks = os.path.join(self.root, "blocks")
        os.makedirs(self.blocks)
        modify_env(cache_dir=self.root + os.sep, block_dir=self.blocks + os.sep)
        self.addCleanup(modify_env, cache_dir=packer.CACHE, block_dir=packer.BLOCKDIR)
        self._blockdir, packer.BLOCKDIR = packer.BLOCKDIR, self.blocks + os.sep
        self.addCleanup(setattr, packer, "BLOCKDIR", self._blockdir)

    def _tree(self, omit_types: bool):
        """What parseFilesys builds: a tree whose large file is a pointer."""
        big_path = os.path.join(self.root, "big.bin")
        with open(big_path, "wb") as f:
            f.write(BIG)
        block_hash, _ = block_builder.create_block(file_path=big_path, copy=True)

        filesystem = celaut.Service.Container.Filesystem()
        small = filesystem.branch.add()
        small.name = "small.txt"
        small.file = SMALL
        big = filesystem.branch.add()
        big.name = "big.bin"
        big.file = block_pointer(
            block_id=block_hash, omit_types=omit_types).SerializeToString()
        return filesystem, [block_hash]

    def _service(self, omit_types: bool = True):
        """What save() writes: a typed pointer to a block built the packer's way."""
        filesystem, blocks = self._tree(omit_types)
        block_id = packer._install_as_block(*block_builder.build_multiblock(
            filesystem, blocks,
            inherited=hash_types_for_packing() if omit_types else None))
        service = celaut.Service()
        service.container.filesystem = block_pointer(
            block_id=block_id).SerializeToString()
        return service, block_id

    # -- the context ----------------------------------------------------
    def test_an_inline_filesystem_inherits_nothing(self):
        filesystem, _ = self._tree(omit_types=False)
        service = celaut.Service()
        service.container.filesystem = filesystem.SerializeToString()
        self.assertIsNone(filesystem_hash_types(service),
                          "an inline tree is part of the spec, so it is the top")

    def test_a_filesystem_block_passes_its_own_types_down(self):
        service, _ = self._service()
        self.assertEqual(filesystem_hash_types(service), (Enviroment.hash_type,))

    def test_the_spec_pointer_states_its_type(self):
        service, block_id = self._service()
        pointer = buffer_pb2.Buffer.Block()
        pointer.ParseFromString(service.container.filesystem)
        self.assertTrue(all(h.type for h in pointer.hashes))
        # Readable with no context at all, which is the point of it.
        self.assertEqual(filesystem_block_id(service.container.filesystem),
                         block_id.hex())

    # -- reading the compressed pointers back ---------------------------
    def test_a_per_file_pointer_needs_the_context_it_inherits(self):
        service, _ = self._service()
        big = {b.name: b for b in load_container_filesystem(service).branch}["big.bin"]
        pointer = buffer_pb2.Buffer.Block()
        pointer.ParseFromString(big.file)
        self.assertFalse(any(h.type for h in pointer.hashes))

        self.assertIsNotNone(
            filesystem_block_id(big.file, inherited=filesystem_hash_types(service)))

    def test_the_content_still_comes_back(self):
        # What ch/build.py's _write_item does, now with the context threaded to it.
        service, _ = self._service()
        big = {b.name: b for b in load_container_filesystem(service).branch}["big.bin"]
        target = os.path.join(self.root, "restored.bin")
        self.assertTrue(copy_block_if_exists(
            buffer=big.file, directory=target,
            inherited=filesystem_hash_types(service)))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), BIG)

    # -- and it costs nothing -------------------------------------------
    def test_the_saving_does_not_rename_the_filesystem_block(self):
        """Which is what makes it safe to adopt: a pointer is replaced by its
        block's content in the expansion, so what the pointer looked like never
        reaches the hash. The filesystem block keeps its id, and so does the
        service above it."""
        spelled_out, spelled_out_id = self._service(omit_types=False)
        inherited, inherited_id = self._service(omit_types=True)
        self.assertEqual(spelled_out_id, inherited_id)
        self.assertEqual(spelled_out.container.filesystem,
                         inherited.container.filesystem)

    def test_the_stored_tree_is_smaller(self):
        spelled_out, _ = self._tree(omit_types=False)
        inherited, _ = self._tree(omit_types=True)
        self.assertEqual(
            len(spelled_out.SerializeToString()) - len(inherited.SerializeToString()),
            len(Enviroment.hash_type) + 2,          # the type, its tag and its length
        )


class PackingMemoryEstimateTests(unittest.TestCase):
    """The reservation follows what a pack actually holds.

    Files at or over MIN_BUFFER_BLOCK_SIZE are streamed into blocks on disk, so
    an image's total size predicts nothing: measured across rootfs shapes it
    ranged from 0.6x to 6.3x of peak memory, while the inlined bytes held steady
    near 5.9x.
    """

    def test_it_grows_with_the_inlined_bytes(self):
        self.assertGreater(packer.packing_memory_estimate(inline_len=200_000_000),
                           packer.packing_memory_estimate(inline_len=100_000_000))

    def test_it_grows_with_the_number_of_blocks(self):
        # A low threshold makes almost every file a block, and then this is the
        # term that matters -- nothing is inlined to be proportional to.
        self.assertGreater(packer.packing_memory_estimate(inline_len=0, block_count=2000),
                           packer.packing_memory_estimate(inline_len=0, block_count=100))

    def test_the_shipped_factor_covers_the_measured_peaks(self):
        # 200 MB inlined peaked at 1183 MB; 30 MB at 188 MB; a real 886 MB image
        # at 4950 MB. Pinned to the shipped defaults rather than read from the
        # config, so this states what those defaults are for and does not turn
        # into a test of whatever the host happens to be configured with.
        with mock.patch.object(packer, "PACKER_MEMORY_SIZE_FACTOR", 6.0), \
                mock.patch.object(packer, "PACKER_MEMORY_PER_BLOCK", 10_000), \
                mock.patch.object(packer, "PACKER_MEMORY_OVERHEAD", 40_000_000):
            # (inlined bytes, blocks, peak measured for that shape)
            for inline, blocks, measured_peak in (
                    (30_000_000, 0, 188_000_000),
                    (200_000_000, 0, 1_183_000_000),
                    (886_000_000, 13, 4_950_000_000),
                    # nothing inlined: all of it is per-block plus the floor
                    (0, 100, 34_200_000),
                    (0, 400, 36_800_000),
                    (0, 1_600, 44_400_000)):
                with self.subTest(inline=inline, blocks=blocks):
                    self.assertGreaterEqual(
                        packer.packing_memory_estimate(
                            inline_len=inline, block_count=blocks),
                        measured_peak)

    def test_the_terms_are_configurable(self):
        with mock.patch.object(packer, "PACKER_MEMORY_SIZE_FACTOR", 3.0), \
                mock.patch.object(packer, "PACKER_MEMORY_PER_BLOCK", 1_000), \
                mock.patch.object(packer, "PACKER_MEMORY_OVERHEAD", 0):
            self.assertEqual(
                packer.packing_memory_estimate(inline_len=1_000_000, block_count=7),
                3_007_000)

    def test_a_filesystem_of_only_large_files_still_reserves_something(self):
        # The worker interpreter is itself tens of MB, before anything is packed.
        self.assertGreater(packer.packing_memory_estimate(inline_len=0), 0)


if __name__ == "__main__":
    unittest.main()

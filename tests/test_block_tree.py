"""A packed dependency must carry the blocks that its blocks name.

bee-rpc stores a layer that holds a shared block as a multiblock directory in
the block directory: ``<BLOCKDIR>/<block>/_.json`` names a second block. The
packer copied only the blocks that the dependency's own ``_.json`` names, so
the parent service held the dependency without the second block. The parent
could not send the dependency to a node that did not have it: bee-rpc stopped
with "gRPCbb: Error reading block." (celaut-basics/sort-sat-solver#6).

These tests check ``block_tree`` and the three places that copy a dependency:
the pack of a service with dependencies, ``nodo ggconf`` and the bundle for a
remote packer.
"""
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from tests.config_bootstrap import load_example_config
load_example_config()

from src.utils.block_tree import block_tree, manifest_block_ids
from src.commands.packer.zip_with_dockerfile import generate_service_zip
from src.commands.packer.zip_with_dockerfile.packer_service_client import build_dependency_bundle


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _multiblock(directory, parts):
    """A multiblock directory: `parts` is a list of file bodies (bytes) and block ids (str)."""
    os.makedirs(directory, exist_ok=True)
    manifest = []
    for i, part in enumerate(parts, start=1):
        if isinstance(part, bytes):
            _write(os.path.join(directory, str(i)), part)
            manifest.append(i)
        else:
            manifest.append([part, [0, 1]])
    with open(os.path.join(directory, "_.json"), "w") as f:
        json.dump(manifest, f)


class StorageCase(unittest.TestCase):
    """A registry with service `dep`: its `_.json` names the layer blocks `top1`
    and `top2`. Both are multiblock directories that name the shared block
    `base` (one file). `top2` also names `mid`, which names `leaf`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.services = os.path.join(root, "registry") + "/"
        self.metadata = os.path.join(root, "metadata") + "/"
        self.blocks = os.path.join(root, "blocks") + "/"
        os.makedirs(self.metadata)
        _multiblock(self.services + "dep", [b"head", "top1", b"mid", "top2", b"tail"])
        _write(self.metadata + "dep", b"meta")
        _multiblock(self.blocks + "top1", [b"a", "base", b"b"])
        _multiblock(self.blocks + "top2", [b"c", "base", b"d", "mid", b"e"])
        _multiblock(self.blocks + "mid", [b"f", "leaf"])
        _write(self.blocks + "base", b"shared layer")
        _write(self.blocks + "leaf", b"leaf")
        _write(self.blocks + "other", b"not used by dep")

    def tearDown(self):
        self.tmp.cleanup()


class BlockTreeTest(StorageCase):

    def test_manifest_block_ids(self):
        self.assertEqual(manifest_block_ids(self.services + "dep"), ["top1", "top2"])

    def test_one_file_block_names_no_blocks(self):
        self.assertEqual(manifest_block_ids(self.blocks + "base"), [])

    def test_missing_directory_names_no_blocks(self):
        self.assertEqual(manifest_block_ids(self.blocks + "ghost"), [])

    def test_every_depth_once_parent_first(self):
        self.assertEqual(
            block_tree(self.services + "dep", self.blocks),
            ["top1", "base", "top2", "mid", "leaf"],
        )

    def test_missing_block_is_listed_without_children(self):
        os.remove(self.blocks + "mid/_.json")
        self.assertEqual(block_tree(self.services + "dep", self.blocks), ["top1", "base", "top2", "mid"])

    def test_cycle_ends(self):
        _multiblock(self.blocks + "leaf2", [b"g", "top2"])
        _multiblock(self.blocks + "mid", [b"f", "leaf2"])
        self.assertEqual(
            block_tree(self.services + "dep", self.blocks),
            ["top1", "base", "top2", "mid", "leaf2"],
        )


class ExportRegistryTest(StorageCase):
    """The pack of a service with dependencies (generate_service_zip)."""

    def test_copies_nested_blocks(self):
        project = os.path.join(self.tmp.name, "project")
        directory = os.path.join(project, ".service", "service")
        os.makedirs(directory)
        pack_config = {"dependencies": {"DEP": "dep"}, "dependencies_env": True}
        with mock.patch.object(generate_service_zip, "SERVICES", self.services), \
                mock.patch.object(generate_service_zip, "METADATA", self.metadata), \
                mock.patch.object(generate_service_zip, "BLOCKS", self.blocks):
            # getattr: in a class body, `module.__export_registry` is name-mangled.
            getattr(generate_service_zip, "__export_registry")(project, directory, pack_config)
        self.assertEqual(
            sorted(os.listdir(os.path.join(directory, "__block__"))),
            ["base", "leaf", "mid", "top1", "top2"],
        )
        self.assertTrue(os.path.isfile(os.path.join(directory, "__block__", "mid", "_.json")))
        self.assertEqual(os.listdir(os.path.join(directory, "__services__")), ["dep"])


class DependencyBundleTest(StorageCase):
    """The bundle that `nodo pack` uploads to a remote packer."""

    def test_bundle_has_nested_blocks(self):
        data = build_dependency_bundle("dep", self.services, self.metadata, self.blocks)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = tf.getnames()
        blocks = sorted({n.split("/")[1] for n in names if n.startswith("blocks/")})
        self.assertEqual(blocks, ["base", "leaf", "mid", "top1", "top2"])


if __name__ == "__main__":
    unittest.main()

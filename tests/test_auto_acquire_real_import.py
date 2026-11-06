"""The automatic path with the real `import_bee` of a real `.celaut.bee` (audit N2, N3, F3).

The other tests of the automatic path patch `import_bee`, so they cannot see what the import
writes to the registry, to the metadata and to the block directory. These build services
with bee-rpc in a directory of their own, export them, serve them from 127.0.0.1, and run
`source_application._download_checked` on them.
"""
import http.server
import io
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from functools import partial
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from bee_rpc import block_builder
    from bee_rpc.utils import block_pointer, modify_env

    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.commands import export_bee as export_module
    from src.commands import import_bee as import_module
    from src.core_services import source_application as sa
    from src.core_services import source_download as sd
    from src.utils.hashing import get_configured_hash_spec
    import src.packers.zip_with_dockerfile as packer
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

WANTED = "ab" * 32
MIB = 1 << 20
# Over the size that bee-rpc puts inline, so that the service has a block in the block directory.
BLOCK_BYTES = 300_000


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RealImportTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nodo-auto-acquire-real-")
        self.addCleanup(shutil.rmtree, self.root, True)
        join = partial(os.path.join, self.root)
        self.blocks, self.cache = join("blocks") + os.sep, join("cache") + os.sep
        self.registry, self.metadata = join("registry") + os.sep, join("metadata") + os.sep
        self.served = join("served")
        for directory in (self.blocks, self.cache, self.registry, self.metadata, self.served):
            os.makedirs(directory)
        modify_env(cache_dir=self.cache, block_dir=self.blocks)
        self.addCleanup(modify_env, cache_dir=packer.CACHE, block_dir=packer.BLOCKDIR)

        real_get = sa._env_manager.get  # the ConfigManager is one object for all modules
        for patcher in (
            patch.object(import_module, "REGISTRY", self.registry),
            patch.object(import_module, "METADATA_REGISTRY", self.metadata),
            patch.object(import_module, "VALIDATE_ON_IMPORT", True),
            patch.object(export_module, "REGISTRY", self.registry),
            patch.object(export_module, "METADATA", self.metadata),
            patch("src.commands.__by_tag.METADATA", self.metadata),
            patch.object(sa._env_manager, "get", side_effect=lambda key, default=None: (
                self.cache if key == "CACHE" else real_get(key, default)
            )),
            patch.object(sd, "address_is_allowed", lambda address: address == "127.0.0.1"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        if hasattr(import_module, "BLOCKDIR"):
            patcher = patch.object(import_module, "BLOCKDIR", self.blocks)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.spec = get_configured_hash_spec()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=self.served))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}/"

    # --- helpers

    def _make_service(self, tag: str, seed: bytes):
        """A service in the registry with one block of its own; its id and its block id."""
        big = os.path.join(self.root, "big.bin")
        with open(big, "wb") as f:
            f.write(seed * BLOCK_BYTES)
        block, _ = block_builder.create_block(file_path=big, copy=True)
        os.remove(big)
        service = celaut.Service()
        service.container.filesystem = block_pointer(block_id=block).SerializeToString()
        service.container.init.entry_path.append("/bin/true")
        _, built = block_builder.build_multiblock(pf_object_with_block_pointers=service, blocks=[block])
        service_id = import_module._compute_service_hash(built, self.spec)
        shutil.move(built, os.path.join(self.registry, service_id))
        self._write_metadata(service_id, tag)
        return service_id, block.hex()

    def _write_metadata(self, service_id: str, tag: str):
        metadata = celaut.Metadata(hashtag=celaut.Metadata.HashTag(
            hash=[celaut.Metadata.HashTag.Hash(type=self.spec.id_bytes, value=bytes.fromhex(service_id))],
            tag=[tag],
        ))
        with open(os.path.join(self.metadata, service_id), "wb") as f:
            f.write(metadata.SerializeToString())

    def _tags(self, service_id: str):
        metadata = celaut.Metadata()
        with open(os.path.join(self.metadata, service_id), "rb") as f:
            metadata.ParseFromString(f.read())
        return list(metadata.hashtag.tag)

    def _export(self, service_id: str, name: str) -> str:
        with redirect_stdout(io.StringIO()):
            export_module.export_bee(service_id, self.served)
        produced = [f for f in os.listdir(self.served) if f.startswith(service_id[:6])]
        os.rename(os.path.join(self.served, produced[0]), os.path.join(self.served, name))
        return self.base + name

    def _forget(self, service_id: str, block_id: str = None):
        """The node does not hold ``service_id`` (nor its block) any more."""
        shutil.rmtree(os.path.join(self.registry, service_id))
        os.remove(os.path.join(self.metadata, service_id))
        if block_id:
            path = os.path.join(self.blocks, block_id)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)

    def _download_checked(self, url: str, service_id: str) -> bool:
        with redirect_stdout(io.StringIO()):
            return sa._download_checked(url, service_id, 50 * MIB)

    # --- the service that was asked for

    def test_the_service_that_was_asked_for_is_imported(self):
        wanted, block = self._make_service("wanted", b"W")
        url = self._export(wanted, "wanted.celaut.bee")
        self._forget(wanted, block)

        self.assertTrue(self._download_checked(url, wanted))

        self.assertEqual(os.listdir(self.registry), [wanted])
        self.assertEqual(self._tags(wanted), ["wanted"])
        self.assertIn(block, os.listdir(self.blocks))
        self.assertEqual(os.listdir(os.path.join(self.cache, "source_downloads")), [])

    # --- audit N3

    def test_a_source_that_serves_a_service_that_the_node_holds_does_not_change_its_tags(self):
        held, _ = self._make_service("other", b"O")
        self._write_metadata(held, "evil")
        url = self._export(held, "evil.celaut.bee")
        self._write_metadata(held, "other")  # what the node has

        self.assertFalse(self._download_checked(url, WANTED))

        self.assertEqual(self._tags(held), ["other"])
        self.assertEqual(os.listdir(self.registry), [held])
        self.assertEqual(os.listdir(self.metadata), [held])

    def test_the_wanted_service_that_arrived_meanwhile_keeps_its_tags(self):
        wanted, _ = self._make_service("evil", b"W")
        url = self._export(wanted, "wanted.celaut.bee")
        self._write_metadata(wanted, "mine")  # a peer gave it with these tags in the meantime
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                import_module.import_bee(
                    os.path.join(self.served, "wanted.celaut.bee"), integrity_hash=wanted, keep_metadata=True
                ),
                wanted,
            )
            self.assertTrue(self._download_checked(url, wanted))
        self.assertEqual(self._tags(wanted), ["mine"])

    def test_metadata_that_names_a_held_service_does_not_keep_the_content_out_of_the_registry(self):
        # The file says it is `held` (that the node has), and its content is `wanted`. Whether
        # the registry holds the service was read from the id that the metadata says, so the
        # content was thrown away and the import still said `wanted`.
        held, _ = self._make_service("held", b"H")
        wanted, block = self._make_service("wanted", b"W")
        with open(os.path.join(self.metadata, wanted), "wb") as f:
            f.write(celaut.Metadata(hashtag=celaut.Metadata.HashTag(
                hash=[celaut.Metadata.HashTag.Hash(type=self.spec.id_bytes, value=bytes.fromhex(held))],
            )).SerializeToString())
        url = self._export(wanted, "lies.celaut.bee")
        self._forget(wanted, block)

        self.assertTrue(self._download_checked(url, wanted))

        self.assertTrue(os.path.isdir(os.path.join(self.registry, wanted)))
        self.assertEqual(self._tags(held), ["held"])

    # --- audit N2

    def test_a_service_that_another_thread_saves_during_the_import_is_kept(self):
        other, block = self._make_service("other", b"O")
        url = self._export(other, "other.celaut.bee")
        self._forget(other, block)
        concurrent = "cd" * 32
        real_import = import_module.import_bee

        def import_while_a_gateway_thread_saves(path, *args, **kwargs):
            # For example a StartService that carried its service, saved in the meantime.
            os.makedirs(os.path.join(self.registry, concurrent))
            open(os.path.join(self.metadata, concurrent), "wb").close()
            return real_import(path, *args, **kwargs)

        with patch.object(import_module, "import_bee", side_effect=import_while_a_gateway_thread_saves):
            self.assertFalse(self._download_checked(url, WANTED))

        self.assertEqual(os.listdir(self.registry), [concurrent])
        self.assertEqual(os.listdir(self.metadata), [concurrent])


if __name__ == "__main__":
    unittest.main()

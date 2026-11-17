"""A node that keeps services without their filesystem (``misc.OMIT_FS``).

These use real bee-rpc services, real ``.celaut.bee`` files and the real ``import_bee``, in
a directory of their own, as ``test_auto_acquire_real_import.py`` does. The ones at the end go
through a real gRPC ``GetService``, as ``test_get_service_block_skip_e2e.py`` does.
"""
import io
import os
import shutil
import tempfile
import unittest
from concurrent import futures
from contextlib import redirect_stdout
from functools import partial
from unittest.mock import patch

IMPORT_ERROR = None
try:
    import grpc
    from bee_rpc import block_builder
    from bee_rpc.block_driver import WITHOUT_BLOCK_POINTERS_FILE_NAME
    from bee_rpc.client import client_grpc, parse_from_buffer
    from bee_rpc.reader import read_bee_file
    from bee_rpc.utils import block_pointer, modify_env

    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from protos import celaut_pb2_grpc
    from protos.gateway_bee import GetService_output_message_mode, rpc_input, rpc_output
    from src.commands import export_bee as export_module
    from src.commands import import_bee as import_module
    from src.commands import storage as storage_module
    from src.gateway.gateway import Gateway
    from src.identity.grpc_transport import node_channel, server_credentials
    from src.identity.node_identity import get_node_public_key_hex
    from src.manager import integrity as integrity_module
    from src.utils import container_filesystem, omit_filesystem, partial_service
    from src.utils import utils as nodo_utils
    from src.utils.hashing import get_configured_hash_id, get_configured_hash_spec
    from src.utils.service_content import read_service_content
    import src.gateway.iterables.abstract_input_service_iterable as abstract_input_service_iterable
    import src.gateway.utils as gateway_utils
    import src.packers.zip_with_dockerfile as packer
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    grpc = None  # type: ignore[assignment]

BLOCK_BYTES = 300_000


class Quiet:
    def __enter__(self):
        self._redirect = redirect_stdout(io.StringIO())
        self._redirect.__enter__()
        return self

    def __exit__(self, *exc):
        return self._redirect.__exit__(*exc)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class Fixture(unittest.TestCase):
    """A node in a directory of its own: registry, metadata and blocks."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nodo-omit-fs-")
        self.addCleanup(shutil.rmtree, self.root, True)
        join = partial(os.path.join, self.root)
        self.blocks, self.cache = join("blocks") + os.sep, join("cache") + os.sep
        self.registry, self.metadata = join("registry") + os.sep, join("metadata") + os.sep
        self.served = join("served")
        for directory in (self.blocks, self.cache, self.registry, self.metadata, self.served):
            os.makedirs(directory)
        modify_env(cache_dir=self.cache, block_dir=self.blocks)
        self.addCleanup(modify_env, cache_dir=packer.CACHE, block_dir=packer.BLOCKDIR)

        for patcher in (
            patch.object(import_module, "REGISTRY", self.registry),
            patch.object(import_module, "METADATA_REGISTRY", self.metadata),
            patch.object(import_module, "VALIDATE_ON_IMPORT", True),
            patch.object(import_module, "BLOCKDIR", self.blocks),
            patch.object(export_module, "REGISTRY", self.registry),
            patch.object(export_module, "METADATA", self.metadata),
            patch.object(partial_service, "REGISTRY", self.registry),
            patch.object(partial_service, "BLOCKDIR", self.blocks),
            patch.object(storage_module, "REGISTRY", self.registry),
            patch.object(storage_module, "BLOCKDIR", self.blocks),
            patch.object(nodo_utils, "REGISTRY", self.registry),
            patch.object(nodo_utils, "METADATA_REGISTRY", self.metadata),
            patch("src.commands.__by_tag.METADATA", self.metadata),
            patch.object(storage_module, "_live_service_ids", lambda: set()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.spec = get_configured_hash_spec()

    # --- helpers

    def make_service(self, tag: str, seed: bytes, entry: str = "/bin/true", blocks: int = 1):
        """A complete service with ``blocks`` blocks of its own, as the packer stores one."""
        service = celaut.Service()
        service.container.init.entry_path.append(entry)
        block_ids = []
        for index in range(blocks):
            big = os.path.join(self.root, "big.bin")
            with open(big, "wb") as f:
                f.write(seed * (BLOCK_BYTES + index))
            block, _ = block_builder.create_block(file_path=big, copy=True)
            os.remove(big)
            block_ids.append(block)
        if block_ids:
            service.container.filesystem = block_pointer(block_id=block_ids[0]).SerializeToString()
        _, built = block_builder.build_multiblock(
            pf_object_with_block_pointers=service, blocks=block_ids)
        service_id = import_module._compute_service_hash(built, self.spec)
        shutil.move(built, os.path.join(self.registry, service_id))
        metadata = celaut.Metadata(hashtag=celaut.Metadata.HashTag(
            hash=[celaut.Metadata.HashTag.Hash(type=self.spec.id_bytes, value=bytes.fromhex(service_id))],
            tag=[tag],
        ))
        with open(os.path.join(self.metadata, service_id), "wb") as f:
            f.write(metadata.SerializeToString())
        return service_id, [b.hex() for b in block_ids]

    def export(self, service_id: str, name: str = "service.celaut.bee") -> str:
        with Quiet():
            export_module.export_bee(service_id, self.served)
        produced = [f for f in os.listdir(self.served) if f.startswith(service_id[:6])]
        path = os.path.join(self.served, name)
        os.rename(os.path.join(self.served, produced[0]), path)
        return path

    def forget(self, service_id: str, block_ids=()):
        """The node does not hold the service, nor its blocks."""
        shutil.rmtree(os.path.join(self.registry, service_id))
        os.remove(os.path.join(self.metadata, service_id))
        for block in block_ids:
            path = os.path.join(self.blocks, block)
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)

    def do_import(self, path: str, **kwargs):
        with Quiet():
            return import_module.import_bee(path, **kwargs)

    def is_partial(self, service_id: str) -> bool:
        return os.path.isfile(os.path.join(self.registry, service_id, ".nofs"))

    def spec_of(self, service_id: str):
        service = celaut.Service()
        with open(os.path.join(self.registry, service_id, WITHOUT_BLOCK_POINTERS_FILE_NAME), "rb") as f:
            service.ParseFromString(f.read())
        return service


class ImportTests(Fixture):
    def test_a_verified_import_stores_no_filesystem(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)

        self.assertEqual(self.do_import(bee, omit_fs=True), service_id)

        self.assertTrue(self.is_partial(service_id))
        self.assertEqual(os.listdir(self.blocks), [])
        self.assertEqual(self.spec_of(service_id).container.init.entry_path, ["/bin/true"])
        self.assertTrue(os.path.isfile(os.path.join(self.metadata, service_id)))

    def test_the_specification_of_a_partial_service_loads(self):
        service_id, blocks = self.make_service("svc", b"S", entry="/bin/partial")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        self.do_import(bee, omit_fs=True)

        service = nodo_utils.load_service_from_disk(service_id)

        self.assertEqual(list(service.container.init.entry_path), ["/bin/partial"])

    def test_the_filesystem_of_a_partial_service_is_not_stored_and_says_so(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        self.do_import(bee, omit_fs=True)

        with self.assertRaises(container_filesystem.FilesystemNotStored):
            container_filesystem.load_container_filesystem(self.spec_of(service_id))

    def test_a_wrong_id_keeps_nothing(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)

        self.assertIsNone(self.do_import(bee, omit_fs=True, integrity_hash="ab" * 32))

        self.assertEqual(os.listdir(self.registry), [])
        self.assertEqual(os.listdir(self.blocks), [])

    def test_a_changed_filesystem_is_refused(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        with open(bee, "rb") as f:
            data = bytearray(f.read())
        position = data.rindex(b"S" * 1000)
        data[position] = ord("X")
        with open(bee, "wb") as f:
            f.write(data)

        # The id of the content is not the one that was asked for.
        self.assertIsNone(self.do_import(bee, omit_fs=True, integrity_hash=service_id))

        self.assertEqual(os.listdir(self.registry), [])
        self.assertEqual(os.listdir(self.blocks), [])

    def test_without_validation_the_filesystem_is_skipped_and_the_id_is_not_checked(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)

        with patch.object(import_module, "VALIDATE_ON_IMPORT", False):
            self.assertEqual(self.do_import(bee, omit_fs=True), service_id)

        self.assertTrue(self.is_partial(service_id))
        self.assertEqual(os.listdir(self.blocks), [])

    def test_the_option_of_the_node_is_the_default(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)

        with patch.object(partial_service.env_manager, "get", side_effect=lambda k, d=None: True if k == "OMIT_FS" else d):
            self.assertEqual(self.do_import(bee), service_id)

        self.assertTrue(self.is_partial(service_id))

    def test_with_the_option_off_the_service_is_complete(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)

        self.assertEqual(self.do_import(bee, omit_fs=False), service_id)

        self.assertFalse(self.is_partial(service_id))
        self.assertEqual(sorted(os.listdir(self.blocks)), sorted(blocks))

    def test_a_service_with_no_block_is_complete(self):
        service_id, _ = self.make_service("small", b"", blocks=0)
        bee = self.export(service_id)
        self.forget(service_id)

        self.assertEqual(self.do_import(bee, omit_fs=True), service_id)

        self.assertFalse(self.is_partial(service_id))

    def test_a_complete_service_stays_complete(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)

        self.assertEqual(self.do_import(bee, omit_fs=True), service_id)

        self.assertFalse(self.is_partial(service_id))
        self.assertEqual(sorted(os.listdir(self.blocks)), sorted(blocks))

    def test_an_import_with_the_option_off_completes_a_partial_service(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        self.do_import(bee, omit_fs=True)
        self.assertTrue(self.is_partial(service_id))

        self.assertEqual(self.do_import(bee, omit_fs=False), service_id)

        self.assertFalse(self.is_partial(service_id))
        self.assertEqual(sorted(os.listdir(self.blocks)), sorted(blocks))
        self.assertEqual(
            import_module._compute_service_hash(os.path.join(self.registry, service_id), self.spec),
            service_id,
        )

    def test_an_import_with_the_option_on_leaves_a_partial_service_partial(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        self.do_import(bee, omit_fs=True)

        self.do_import(bee, omit_fs=True)

        self.assertTrue(self.is_partial(service_id))
        self.assertEqual(os.listdir(self.blocks), [])

    def test_the_length_comes_from_the_field_when_the_sender_skips_the_body(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)

        def without_bodies():
            depth = 0
            for buffer in read_bee_file(bee):
                if buffer.HasField("block"):
                    depth = 0 if depth else 1
                    yield buffer
                elif depth and buffer.HasField("chunk"):
                    continue  # what the sender does not send after the skip request
                else:
                    yield buffer

        session = omit_filesystem.OmitSession(service_index=2)
        with omit_filesystem.omit_session(session):
            it = parse_from_buffer(
                request_iterator=session.tap(without_bodies()),
                indices={1: celaut.Metadata, 2: celaut.Service},
                partitions_message_mode=False,
            )
            next(it)
            service_dir = next(it).dir

        self.assertEqual(list(session.lengths.values()), [BLOCK_BYTES])
        self.assertEqual(os.listdir(self.blocks), [])
        self.assertTrue(os.path.isfile(os.path.join(service_dir, WITHOUT_BLOCK_POINTERS_FILE_NAME)))

    def test_without_a_session_bee_rpc_works_as_before(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)

        omit_filesystem.install()
        self.assertEqual(self.do_import(bee, omit_fs=False), service_id)

        self.assertEqual(sorted(os.listdir(self.blocks)), sorted(blocks))


class RefusalTests(Fixture):
    def partial_service(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        self.do_import(bee, omit_fs=True)
        return service_id

    def test_a_partial_service_is_not_exported(self):
        service_id = self.partial_service()
        for name in os.listdir(self.served):
            os.remove(os.path.join(self.served, name))
        out = io.StringIO()

        with redirect_stdout(out):
            export_module.export_bee(service_id, self.served)

        self.assertIn("partial", out.getvalue())
        self.assertEqual(os.listdir(self.served), [])

    def test_the_integrity_check_shows_a_partial_service_and_does_not_report_damage(self):
        service_id = self.partial_service()
        real_get = integrity_module.ConfigManager().get

        def get(key, default=None):
            return {"REGISTRY": self.registry, "METADATA_REGISTRY": self.metadata}.get(key) or real_get(key, default)

        with patch.object(integrity_module.ConfigManager, "get", side_effect=get):
            report = integrity_module.check_integrity()

        self.assertEqual(report["partial"], [service_id])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["checked"], 0)

    def test_an_operation_that_needs_the_filesystem_says_why(self):
        service_id = self.partial_service()

        self.assertTrue(partial_service.is_partial(service_id))
        with self.assertRaises(partial_service.PartialServiceError) as raised:
            partial_service.require_complete(service_id, "run it")
        self.assertIn("nodo download", str(raised.exception))


class PruneTests(Fixture):
    def setUp(self):
        super().setUp()
        self.omit = patch.object(
            partial_service.env_manager, "get",
            side_effect=lambda key, default=None: True if key == "OMIT_FS" else default,
        )

    def prune(self, omit: bool):
        with Quiet():
            if omit:
                with self.omit:
                    storage_module.prune_blocks()
            else:
                storage_module.prune_blocks()

    def test_turning_the_option_on_removes_nothing(self):
        service_id, blocks = self.make_service("svc", b"S")

        with self.omit:
            self.assertTrue(partial_service.omit_fs_enabled())

        self.assertFalse(self.is_partial(service_id))
        self.assertEqual(sorted(os.listdir(self.blocks)), sorted(blocks))

    def test_prune_drops_the_filesystem_and_writes_the_marker(self):
        service_id, blocks = self.make_service("svc", b"S")

        self.prune(omit=True)

        self.assertTrue(self.is_partial(service_id))
        self.assertEqual(os.listdir(self.blocks), [])
        self.assertEqual(self.spec_of(service_id).container.init.entry_path, ["/bin/true"])

    def test_prune_with_the_option_off_changes_no_service(self):
        service_id, blocks = self.make_service("svc", b"S")

        self.prune(omit=False)

        self.assertFalse(self.is_partial(service_id))
        self.assertEqual(sorted(os.listdir(self.blocks)), sorted(blocks))

    def test_prune_keeps_a_service_with_no_block_complete(self):
        service_id, _ = self.make_service("small", b"", blocks=0)

        self.prune(omit=True)

        self.assertFalse(self.is_partial(service_id))

    def test_a_service_with_a_live_instance_keeps_its_filesystem(self):
        live, live_blocks = self.make_service("live", b"L")
        other, other_blocks = self.make_service("other", b"O")

        with patch.object(storage_module, "_live_service_ids", lambda: {live}):
            self.prune(omit=True)

        self.assertFalse(self.is_partial(live))
        self.assertTrue(self.is_partial(other))
        self.assertEqual(os.listdir(self.blocks), live_blocks)

    def test_a_block_that_a_complete_service_needs_stays(self):
        live, blocks = self.make_service("live", b"S", entry="/bin/true")
        other, same_blocks = self.make_service("other", b"S", entry="/bin/false")
        self.assertEqual(blocks, same_blocks)

        with patch.object(storage_module, "_live_service_ids", lambda: {live}):
            self.prune(omit=True)

        self.assertTrue(self.is_partial(other))
        self.assertEqual(os.listdir(self.blocks), blocks)

    def test_the_marker_is_written_before_a_block_is_removed(self):
        service_id, _ = self.make_service("svc", b"S")
        seen = []

        def fail(path, *args, **kwargs):
            seen.append(self.is_partial(service_id))
            raise OSError("disk error")

        with patch.object(storage_module.shutil, "rmtree", fail), patch.object(
                storage_module.os, "remove", fail):
            with self.assertRaises(OSError):
                self.prune(omit=True)

        self.assertEqual(seen, [True])
        self.assertTrue(self.is_partial(service_id))

    def test_an_unreadable_service_removes_nothing(self):
        service_id, blocks = self.make_service("svc", b"S")
        broken = os.path.join(self.registry, "broken")
        os.makedirs(broken)  # a service directory with no _.json

        with self.assertRaises(Exception):
            self.prune(omit=True)

        self.assertEqual(sorted(os.listdir(self.blocks)), sorted(blocks))
        self.assertFalse(self.is_partial(service_id))

    def test_prune_removes_a_block_that_only_a_partial_service_names(self):
        service_id, blocks = self.make_service("svc", b"S")
        partial_service.mark_partial(service_id)  # it names the block, and does not use it

        self.prune(omit=False)

        self.assertEqual(os.listdir(self.blocks), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FetchFromPeersTests(Fixture):
    """``fetch_service_from_peers`` with the option on. ``GetService`` is stood in for by the
    stream of a real ``.celaut.bee`` file, which goes through the tap as the answer of a peer
    does."""

    def setUp(self):
        super().setUp()
        from src.manager import maintain
        self.maintain = maintain
        for name, value in (
            ("REGISTRY", self.registry), ("METADATA_REGISTRY", self.metadata),
            ("GET_SERVICE_SERVICE_INDEX", 2), ("_get_service_penalised", set()),
        ):
            patcher = patch.object(maintain, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("get_client_id_on_other_peer", "peer_channel"):
            patcher = patch.object(maintain, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(maintain, "_get_service_channel", side_effect=lambda peer: (peer, None))
        patcher.start()
        self.addCleanup(patcher.stop)
        reputation = patch.object(maintain, "_reputation_interface")
        self.reputation = reputation.start().return_value
        self.addCleanup(reputation.stop)
        patcher = patch.object(maintain, "peers_id_iterator", side_effect=lambda: iter(["peer-a"]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings(self, validate: bool, omit: bool = True):
        real = self.maintain.env_manager.get
        values = {"OMIT_FS": omit, "VALIDATE_ON_IMPORT": validate}

        def get(key, default=None):
            return values[key] if key in values else real(key, default)

        for target in (self.maintain.env_manager, partial_service.env_manager):
            patcher = patch.object(target, "get", side_effect=get)
            patcher.start()
            self.addCleanup(patcher.stop)

    def peer_has(self, bee: str):
        def get_service(channel, _hash, client_id="", timeout=None, tap=None, block_skip=True):
            stream = read_bee_file(bee)
            if tap is not None:
                stream = tap(stream)
            return parse_from_buffer(
                request_iterator=stream, indices={1: celaut.Metadata, 2: celaut.Service},
                partitions_message_mode={1: True, 2: False},
            )

        self.calls = []
        patcher = patch.object(self.maintain.BeeClient, "get_service", side_effect=lambda *a, **k: (
            self.calls.append(k) or get_service(*a, **k)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_verified_service_is_stored_without_its_filesystem(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        self.settings(validate=True)
        self.peer_has(bee)

        self.assertTrue(self.maintain.fetch_service_from_peers(service_id))

        self.assertTrue(self.is_partial(service_id))
        self.assertEqual(os.listdir(self.blocks), [])
        self.assertFalse(self.calls[0]["block_skip"])  # the whole service is hashed
        self.assertIsNotNone(self.calls[0]["tap"])

    def test_a_changed_filesystem_is_dropped_and_the_peer_penalised(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        with open(bee, "rb") as f:
            data = bytearray(f.read())
        data[data.rindex(b"S" * 1000)] = ord("X")
        with open(bee, "wb") as f:
            f.write(data)
        self.settings(validate=True)
        self.peer_has(bee)

        self.assertFalse(self.maintain.fetch_service_from_peers(service_id))

        self.assertFalse(os.path.exists(os.path.join(self.registry, service_id)))
        self.assertEqual(os.listdir(self.blocks), [])
        self.reputation.update_peer_reputation.assert_called_once()

    def test_without_validation_the_peer_is_asked_to_skip(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        self.settings(validate=False)
        self.peer_has(bee)

        self.assertTrue(self.maintain.fetch_service_from_peers(service_id))

        self.assertTrue(self.is_partial(service_id))
        self.assertTrue(self.calls[0]["block_skip"])
        self.assertEqual(os.listdir(self.blocks), [])

    def test_with_the_option_off_the_service_is_complete(self):
        service_id, blocks = self.make_service("svc", b"S")
        bee = self.export(service_id)
        self.forget(service_id, blocks)
        self.settings(validate=True, omit=False)
        self.peer_has(bee)

        self.assertTrue(self.maintain.fetch_service_from_peers(service_id))

        self.assertFalse(self.is_partial(service_id))
        self.assertIsNone(self.calls[0]["tap"])
        self.assertEqual(sorted(os.listdir(self.blocks)), sorted(blocks))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LaunchTests(unittest.TestCase):
    """A partial service is not run here, and a peer that holds it in full can run it."""

    service_egress = False

    def setUp(self):
        from tests import test_hidden_network_policy as hidden
        self._launch = partial(hidden.LaunchTests._launch, self)
        patcher = patch("src.gateway.launcher.launch_service.is_partial", lambda service_id: service_id == "svc")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_peer_runs_a_partial_service(self):
        r = self._launch(celaut.Service(), ["local", "peer-a"], hidden=False)

        self.assertIsNone(r.error)
        r.local.assert_not_called()
        self.assertEqual(r.delegate.call_args.kwargs["peer"], "peer-a")

    def test_with_no_peer_the_launch_fails_and_says_the_service_is_partial(self):
        r = self._launch(celaut.Service(), ["local"], hidden=False)

        self.assertIsNotNone(r.error)
        self.assertIn("partial", str(r.error))
        r.local.assert_not_called()
        r.spend.assert_not_called()

    def test_a_complete_service_still_runs_here(self):
        with patch("src.gateway.launcher.launch_service.is_partial", lambda service_id: False):
            r = self._launch(celaut.Service(), ["local"], hidden=False)

        self.assertIsNone(r.error)
        r.local.assert_called_once()

    def test_the_quote_has_no_local_offer_for_a_partial_service(self):
        from src.gateway.iterables import estimated_cost_iterable as quote_mod
        iterable = quote_mod.GetServiceEstimatedCostIterable.__new__(
            quote_mod.GetServiceEstimatedCostIterable)
        iterable.service_hash = "svc"
        with patch.object(quote_mod, "executes_locally", return_value=True), \
                patch.object(quote_mod, "is_partial", return_value=True):
            price, refusal = iterable._local_offer()

        self.assertIsNone(price)
        self.assertIn("partial", str(refusal))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GetServiceTests(unittest.TestCase):
    """A real gRPC ``GetService``: what crosses the wire, and what a partial service gives."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nodo-omit-fs-grpc-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.blocks = os.path.join(self.root, "blocks") + os.sep
        self.cache = os.path.join(self.root, "cache") + os.sep
        self.registry = os.path.join(self.root, "registry") + os.sep
        self.metadata_registry = os.path.join(self.root, "metadata") + os.sep
        for directory in (self.blocks, self.cache, self.registry, self.metadata_registry):
            os.makedirs(directory)
        modify_env(cache_dir=self.cache, block_dir=self.blocks)
        self.addCleanup(modify_env, cache_dir=packer.CACHE, block_dir=packer.BLOCKDIR)
        for module in (gateway_utils, abstract_input_service_iterable, nodo_utils):
            original_registry, original_metadata = module.REGISTRY, module.METADATA_REGISTRY
            self.addCleanup(setattr, module, "REGISTRY", original_registry)
            self.addCleanup(setattr, module, "METADATA_REGISTRY", original_metadata)
            module.REGISTRY = self.registry
            module.METADATA_REGISTRY = self.metadata_registry
        patcher = patch.object(partial_service, "REGISTRY", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.hash_id = get_configured_hash_id()
        local_instance = patch("src.gateway.client_gate.get_internal_service_id_by_uri", return_value="self")
        local_instance.start()
        self.addCleanup(local_instance.stop)

        self.grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        celaut_pb2_grpc.add_GatewayServicer_to_server(Gateway(), self.grpc_server)
        port = self.grpc_server.add_secure_port("127.0.0.1:0", server_credentials())
        self.grpc_server.start()
        self.addCleanup(self.grpc_server.stop, None)
        self.gateway = f"127.0.0.1:{port}"
        self.peer_id = get_node_public_key_hex()

    def _register(self, service_hash: str) -> str:
        big = os.path.join(self.root, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(BLOCK_BYTES))
        block, _ = block_builder.create_block(file_path=big, copy=True)
        service = celaut.Service()
        service.container.filesystem = block_pointer(block_id=block).SerializeToString()
        service.container.init.entry_path.append("/entry")
        _, cache_dir = block_builder.build_multiblock(pf_object_with_block_pointers=service, blocks=[block])
        metadata = celaut.Metadata(hashtag=celaut.Metadata.HashTag(
            hash=[celaut.Metadata.HashTag.Hash(type=self.hash_id, value=bytes.fromhex(service_hash))]))
        self.assertTrue(gateway_utils.save_service(
            metadata=metadata, service_dir=cache_dir, service_hash=service_hash))
        return block.hex()

    def _fetch(self, service_hash: str, session=None, block_skip=True):
        channel = node_channel(self.gateway, expected_peer_id=self.peer_id)
        self.addCleanup(channel.close)
        stub = celaut_pb2_grpc.GatewayStub(channel)
        _hash = celaut.Metadata.HashTag.Hash(type=self.hash_id, value=bytes.fromhex(service_hash))
        method = stub.GetService
        if session is not None:
            raw = method

            def method(request_iterator, timeout=None):
                return session.tap(raw(request_iterator, timeout=timeout))

        def run():
            return list(client_grpc(
                method=method,
                indices_serializer=rpc_input("GetService"),
                input=[celaut.Client(client_id="test-fixture"), _hash],
                indices_parser=rpc_output("GetService"),
                partitions_message_mode_parser=dict(GetService_output_message_mode),
                block_skip=block_skip,
                timeout=10.0,
            ))

        if session is None:
            return run()
        with omit_filesystem.omit_session(session):
            return run()

    def test_with_verification_the_session_hashes_the_whole_service(self):
        service_hash = "aa" * 20
        block = self._register(service_hash)

        session = omit_filesystem.OmitSession(service_index=6, spec=get_configured_hash_spec())
        items = self._fetch(service_hash, session=session, block_skip=False)

        self.assertTrue(any(hasattr(item, "dir") for item in items))
        self.assertEqual(session.omitted, [block])
        self.assertEqual(session.lengths[block], BLOCK_BYTES)
        expected = get_configured_hash_spec()
        from src.utils.hashing import hash_stream
        self.assertEqual(
            session.digest(),
            hash_stream(read_service_content(os.path.join(self.registry, service_hash)), expected),
        )

    def test_without_verification_the_peer_is_asked_to_skip_the_filesystem(self):
        service_hash = "bb" * 20
        block = self._register(service_hash)

        attempts = []
        for _ in range(5):
            session = omit_filesystem.OmitSession(service_index=6)
            wire = []

            class Counting(grpc.StreamStreamClientInterceptor):
                def intercept_stream_stream(self, continuation, details, request_iterator):
                    call = continuation(details, request_iterator)

                    def counting():
                        for response in call:
                            wire.append(response.ByteSize())
                            yield response

                    return counting()

            channel = node_channel(self.gateway, expected_peer_id=self.peer_id)
            self.addCleanup(channel.close)
            stub = celaut_pb2_grpc.GatewayStub(grpc.intercept_channel(channel, Counting()))
            raw = stub.GetService
            _hash = celaut.Metadata.HashTag.Hash(type=self.hash_id, value=bytes.fromhex(service_hash))

            def method(request_iterator, timeout=None, raw=raw, session=session):
                return session.tap(raw(request_iterator, timeout=timeout))

            with omit_filesystem.omit_session(session):
                items = list(client_grpc(
                    method=method,
                    indices_serializer=rpc_input("GetService"),
                    input=[celaut.Client(client_id="test-fixture"), _hash],
                    indices_parser=rpc_output("GetService"),
                    partitions_message_mode_parser=dict(GetService_output_message_mode),
                    block_skip=True,
                    timeout=10.0,
                ))
            self.assertTrue(any(hasattr(item, "dir") for item in items))
            self.assertEqual(session.lengths, {block: BLOCK_BYTES})
            attempts.append(sum(wire))
            if sum(wire) < BLOCK_BYTES:
                break

        # A skip is a race: the request may arrive after the body went out. It must
        # be able to save the body, and a skip that never works fails all the tries.
        self.assertLess(min(attempts), BLOCK_BYTES, f"the filesystem was sent every time: {attempts}")

    def test_a_partial_service_is_not_sent_to_a_peer(self):
        service_hash = "cc" * 20
        self._register(service_hash)
        partial_service.mark_partial(service_hash, self.registry)

        items = self._fetch(service_hash)

        self.assertFalse(any(hasattr(item, "dir") for item in items))


if __name__ == "__main__":
    unittest.main()

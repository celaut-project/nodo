"""`nodo pack --fast` / `--optimize` / `packer.fast` (single-block packing).

The local packer (`packer.local: true`) normally splits every file at or over
`packer.MIN_BUFFER_BLOCK_SIZE` into its own content-addressed block under
`main.BLOCKDIR`, inlining everything smaller into the service's filesystem
message. `--fast` skips that entirely -- every file inlines, so the filesystem
ends up as a single block with nothing else in `BLOCKDIR`.

These tests pin the plumbing that carries the `fast` flag from `nodo pack`
down to the actual per-file decision, without running BuildKit:

* `_is_inline` -- the threshold check itself, unit-tested directly;
* `pack_zip` -- forwards `fast` to the worker subprocess as a trailing
  `--fast` argv entry (the worker is a separate process, so it can't be
  passed as a Python kwarg);
* `_worker_main` -- parses that trailing `--fast` back out;
* `pack()` (`pack.py`) -- forwards `fast` to the local packer, and warns +
  ignores it on the (default) remote packer-service backend, which this repo
  does not control the internals of.

`ZipContainerPacker.__init__` runs BuildKit, so `parseFilesys`'s actual block
creation is exercised only through `_is_inline` here -- the same test shape
`test_packer_read_only_filesystem.py` uses for the other init-only-tested
methods.
"""
import os
import tempfile
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    # Before anything that builds a ConfigManager at import: the shipped example
    # points STORAGE at /nodo, which only exists on an installed node.
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.packers import zip_with_dockerfile as packer
    from src.commands.packer.zip_with_dockerfile import pack as pack_mod
    from src.commands.packer.zip_with_dockerfile import local_pack as local_pack_mod
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    packer = None  # type: ignore[assignment]
    pack_mod = None  # type: ignore[assignment]
    local_pack_mod = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class IsInlineTests(unittest.TestCase):
    def test_fast_inlines_regardless_of_size(self):
        huge = packer.MIN_BUFFER_BLOCK_SIZE * 100
        self.assertTrue(packer._is_inline(huge, fast=True))

    def test_normal_mode_follows_the_threshold(self):
        small = packer.MIN_BUFFER_BLOCK_SIZE - 1
        big = packer.MIN_BUFFER_BLOCK_SIZE
        self.assertTrue(packer._is_inline(small, fast=False))
        self.assertFalse(packer._is_inline(big, fast=False))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ZipfileOkForwardsFastTests(unittest.TestCase):
    """zipfile_ok() -> ok(): fast must reach ZipContainerPacker unchanged."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo packer fast test-")
        self.addCleanup(self._tmp.cleanup)
        self.cache = os.path.join(self._tmp.name, "cache") + os.sep
        os.makedirs(self.cache, exist_ok=True)

        cache_patch = mock.patch.object(packer, "CACHE", self.cache)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

        self.ok = mock.Mock(return_value=("serviceid", None, "/dev/null"))
        ok_patch = mock.patch.object(packer, "ok", self.ok)
        ok_patch.start()
        self.addCleanup(ok_patch.stop)

        self.zip_path = os.path.join(self._tmp.name, "service.zip")
        import stat
        import zipfile
        with zipfile.ZipFile(self.zip_path, "w") as z:
            info = zipfile.ZipInfo("service.json")
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            z.writestr(info, "{}")

    def test_fast_true_reaches_ok(self):
        packer.zipfile_ok(zip=self.zip_path, fast=True)
        self.assertTrue(self.ok.call_args.kwargs["fast"])

    def test_fast_defaults_to_false(self):
        packer.zipfile_ok(zip=self.zip_path)
        self.assertFalse(self.ok.call_args.kwargs["fast"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PackZipWorkerCommandTests(unittest.TestCase):
    """pack_zip() spawns the worker subprocess -- fast rides along as argv."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo packer fast test-")
        self.addCleanup(self._tmp.cleanup)
        self.cache = os.path.join(self._tmp.name, "cache") + os.sep
        os.makedirs(self.cache, exist_ok=True)
        cache_patch = mock.patch.object(packer, "CACHE", self.cache)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def _worker_cmd(self, fast):
        # A non-zero returncode short-circuits pack_zip into the "subprocess
        # failed" branch before it ever touches a result file, so no worker
        # process or result JSON needs to exist for this test.
        run_mock = mock.Mock(return_value=mock.Mock(returncode=1))
        with mock.patch("subprocess.run", run_mock):
            list(packer.pack_zip(zip=os.path.join(self._tmp.name, "s.zip"), fast=fast))
        run_mock.assert_called_once()
        return run_mock.call_args.args[0]

    def test_fast_true_appends_the_flag(self):
        cmd = self._worker_cmd(fast=True)
        self.assertIn("--fast", cmd)

    def test_fast_false_omits_the_flag(self):
        cmd = self._worker_cmd(fast=False)
        self.assertNotIn("--fast", cmd)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class WorkerMainParsesFastTests(unittest.TestCase):
    """_worker_main() parses the trailing --fast back out of argv."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo packer fast test-")
        self.addCleanup(self._tmp.cleanup)
        self.result_path = os.path.join(self._tmp.name, "result.json")

        self.zipfile_ok = mock.Mock(return_value=("serviceid", None, "/dev/null"))
        patch_ = mock.patch.object(packer, "zipfile_ok", self.zipfile_ok)
        patch_.start()
        self.addCleanup(patch_.stop)

    def _run(self, extra_argv):
        argv = ["nodo-worker", "--worker", "z.zip", self.result_path] + extra_argv
        with mock.patch.object(packer.sys, "argv", argv):
            packer._worker_main()
        return self.zipfile_ok.call_args.kwargs["fast"]

    def test_trailing_fast_flag_is_parsed(self):
        self.assertTrue(self._run(["--fast"]))

    def test_no_flag_defaults_to_false(self):
        self.assertFalse(self._run([]))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PackDispatchTests(unittest.TestCase):
    """pack() (pack.py): local backend forwards fast; remote backend warns."""

    def test_local_backend_forwards_fast_to_pack_local(self):
        pack_local_mock = mock.Mock(return_value="serviceid")
        with mock.patch.object(pack_mod, "_local_packer_enabled", return_value=True), \
             mock.patch.object(local_pack_mod, "pack_local", pack_local_mock):
            result = pack_mod.pack("some/dir", fast=True)

        self.assertEqual(result, "serviceid")
        pack_local_mock.assert_called_once_with("some/dir", fast=True)

    def test_remote_backend_warns_and_ignores_fast(self):
        via_service = mock.Mock(return_value="serviceid")
        with mock.patch.object(pack_mod, "_local_packer_enabled", return_value=False), \
             mock.patch.object(pack_mod, "_pack_via_service", via_service), \
             mock.patch("builtins.print") as mock_print:
            result = pack_mod.pack("some/dir", fast=True)

        self.assertEqual(result, "serviceid")
        via_service.assert_called_once_with("some/dir")
        self.assertTrue(
            any("--fast" in str(call) for call in mock_print.call_args_list),
            f"expected a --fast warning, got: {mock_print.call_args_list}",
        )

    def test_remote_backend_without_fast_is_silent(self):
        via_service = mock.Mock(return_value="serviceid")
        with mock.patch.object(pack_mod, "_local_packer_enabled", return_value=False), \
             mock.patch.object(pack_mod, "_pack_via_service", via_service), \
             mock.patch("builtins.print") as mock_print:
            pack_mod.pack("some/dir", fast=False)

        mock_print.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PackCommandFlagsTests(unittest.TestCase):
    """`nodo pack` (src/commands/packs.py): --fast / --optimize / packer.fast."""

    def setUp(self):
        from src.commands import packs
        self.packs = packs
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo packer fast test-")
        self.addCleanup(self._tmp.cleanup)
        os.makedirs(os.path.join(self._tmp.name, "proj"))
        env = mock.patch.dict(os.environ, {"ORIGINAL_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _foreground_fast(self, argv, config_fast=False):
        seen = {}

        def foreground(source, kind, as_json=False, local=False, fast=False):
            seen["fast"] = fast
            return True

        with mock.patch.object(self.packs, "foreground", foreground), \
             mock.patch("src.utils.config.ConfigManager.get",
                        side_effect=lambda key, default=None:
                        config_fast if key == "packer.fast" else default):
            self.assertEqual(self.packs.pack_command(["proj", *argv]), 0)
        return seen["fast"]

    def test_fast_flag(self):
        self.assertTrue(self._foreground_fast(["--fast"]))

    def test_default_follows_packer_fast(self):
        self.assertFalse(self._foreground_fast([]))
        self.assertTrue(self._foreground_fast([], config_fast=True))

    def test_optimize_overrides_packer_fast(self):
        self.assertFalse(self._foreground_fast(["--optimize"], config_fast=True))

    def test_fast_and_optimize_are_exclusive(self):
        with mock.patch.object(self.packs, "foreground") as foreground, \
             mock.patch("builtins.print"):
            self.assertEqual(self.packs.pack_command(["proj", "--fast", "--optimize"]), 1)
        foreground.assert_not_called()

    def test_detach_gives_the_flags_to_the_child(self):
        from src.utils import pack_registry
        for argv, expected in ((["--fast"], ["--fast"]), (["--optimize"], ["--optimize"]),
                               (["--local", "--fast"], ["--local", "--fast"]), ([], None)):
            seen = {}

            def spawn_detached(source, nodo_py, **kwargs):
                seen.update(kwargs)
                return {"id": "3f9a0c12", "pid": 1, "source": source, "log": "x.log"}, None

            with mock.patch.object(pack_registry, "spawn_detached", spawn_detached), \
                 mock.patch("builtins.print"):
                self.assertEqual(self.packs.pack_command(["proj", "--detach", *argv]), 0)
            self.assertEqual(seen["options"], expected, argv)

    def test_detach_warns_in_the_parent_that_the_service_ignores_fast(self):
        from src.utils import pack_registry
        spawned = ({"id": "3f9a0c12", "pid": 1, "source": "proj", "log": "x.log"}, None)
        for argv, kind, warned in ((["--fast"], "service", True), (["--fast"], "local", False),
                                   ([], "service", False)):
            with mock.patch.object(pack_registry, "spawn_detached", return_value=spawned), \
                 mock.patch.object(self.packs, "_packer_kind", return_value=kind), \
                 mock.patch("builtins.print") as printed:
                self.assertEqual(self.packs.pack_command(["proj", "--detach", *argv]), 0)
            self.assertEqual(
                any(c.args and c.args[0] == self.packs.FAST_IGNORED
                    for c in printed.call_args_list), warned, (argv, kind))

    def test_service_fallback_to_local_keeps_fast(self):
        with mock.patch.object(pack_mod, "_local_packer_enabled", return_value=False), \
             mock.patch.object(pack_mod, "_pack_via_service",
                               return_value=pack_mod._SERVICE_UNAVAILABLE), \
             mock.patch.object(pack_mod, "_offer_local_packer", return_value=True), \
             mock.patch.object(pack_mod.pack_registry, "use_packer"), \
             mock.patch.object(pack_mod, "_pack_local", return_value="localid") as local, \
             mock.patch("builtins.print"):
            self.assertEqual(pack_mod.pack("some/dir", fast=True), "localid")
        local.assert_called_once_with("some/dir", fast=True)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SameIdsTests(unittest.TestCase):
    """Both modes give the same filesystem block id, metadata hashes and service id.

    Builds the tree the way parseFilesys does (a file inlines or becomes a pointer
    per _is_inline) and the spec the way save() does, without BuildKit.
    """

    def _ids(self, fast):
        import shutil
        from bee_rpc import block_builder, client as grpcbb
        from bee_rpc.utils import modify_env, block_pointer, hash_types_for_packing
        from protos import celaut_pb2 as celaut
        from src.utils.verify import calculate_hashes_by_stream
        from src.utils.hashing import hash_stream_many, get_configured_hash_spec

        root = tempfile.mkdtemp(prefix="nodo-fast-ids-")
        self.addCleanup(shutil.rmtree, root, True)
        blocks = os.path.join(root, "blocks")
        os.makedirs(blocks)
        modify_env(cache_dir=root + os.sep, block_dir=blocks + os.sep)
        self.addCleanup(modify_env, cache_dir=packer.CACHE, block_dir=packer.BLOCKDIR)
        with mock.patch.object(packer, "BLOCKDIR", blocks + os.sep):
            threshold = packer.MIN_BUFFER_BLOCK_SIZE
            files = [("small", b"s" * 300), ("big", bytes(range(256)) * (threshold // 64)),
                     ("big2", b"L" * (threshold + 5)), ("dup", b"L" * (threshold + 5))]
            tree, pointed = celaut.Service.Container.Filesystem(), []
            for name, data in files:
                path = os.path.join(root, name)
                with open(path, "wb") as f:
                    f.write(data)
                branch = tree.branch.add()
                branch.name = name
                if packer._is_inline(len(data), fast):
                    branch.file = data
                else:
                    block_hash, _ = block_builder.create_block(file_path=path, copy=True)
                    branch.file = block_pointer(block_id=block_hash, omit_types=True).SerializeToString()
                    if block_hash not in pointed:
                        pointed.append(block_hash)
            fs_block = packer._install_as_block(*block_builder.build_multiblock(
                pf_object_with_block_pointers=tree, blocks=pointed,
                inherited=hash_types_for_packing()))
            hashtag = calculate_hashes_by_stream(
                value=grpcbb.read_block(block_id=fs_block.hex(), ignore_blocks=True))
            spec = celaut.Service()
            spec.container.filesystem = block_pointer(block_id=fs_block).SerializeToString()
            _, service_dir = block_builder.build_multiblock(
                pf_object_with_block_pointers=spec, blocks=[fs_block])
            hash_spec = get_configured_hash_spec(packer.env_manager)
            service_id = hash_stream_many(
                grpcbb.read_multiblock_directory(directory=service_dir), [hash_spec])[hash_spec.id_bytes]
            return (fs_block, [(h.type, h.value) for h in hashtag], service_id), len(os.listdir(blocks))

    def test_same_ids_fewer_blocks(self):
        normal, normal_blocks = self._ids(fast=False)
        fast, fast_blocks = self._ids(fast=True)
        self.assertEqual(normal, fast)
        self.assertEqual(fast_blocks, 1)
        self.assertGreater(normal_blocks, 1)



@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FastFallbackReasonTests(unittest.TestCase):
    """A fast pack that cannot work packs with per-file blocks instead."""

    def test_small_image_with_room_stays_fast(self):
        self.assertIsNone(packer.fast_fallback_reason(10 << 20, ram_available=1 << 40))

    def test_over_the_size_ceiling(self):
        with mock.patch.object(packer, "FAST_MAX_BYTES", 100 << 20):
            reason = packer.fast_fallback_reason(101 << 20, ram_available=1 << 40)
        self.assertIn("packer.FAST_MAX_BYTES", reason)

    def test_default_ceiling_is_well_under_the_protobuf_limit(self):
        self.assertLessEqual(int(packer.FAST_MAX_BYTES), 1 << 30)

    def test_not_enough_ram_for_the_whole_image(self):
        image = 50 << 20
        needed = packer.packing_memory_estimate(inline_len=image)
        self.assertIn("RAM", packer.fast_fallback_reason(image, ram_available=needed - 1))
        self.assertIsNone(packer.fast_fallback_reason(image, ram_available=needed))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OkFallsBackTests(unittest.TestCase):
    """ok() drops fast before locking memory, and locks the per-file-block amount."""

    IMAGE, SMALL, BLOCKS = 300 << 20, 2 << 20, 40

    def _ok(self, ram_available, max_bytes=1 << 30):
        spec = packer.ZipContainerPacker.__new__(packer.ZipContainerPacker)
        spec.fast, spec.error_msg, spec.buffer_len = True, None, self.IMAGE
        spec._blocked_inline_len, spec._blocked_block_count = self.SMALL, self.BLOCKS
        spec._apply_mode()
        for name in ("parseContainer", "parseApi", "parseNetwork"):
            setattr(spec, name, mock.Mock())
        spec.save = mock.Mock(return_value=("sid", None, "/dir"))
        locked = []

        class Lock:
            def __init__(self, len, timeout=None):
                locked.append(len)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        iobd = mock.Mock()
        iobd.get_ram_avaliable.return_value = ram_available
        notes = {}
        with mock.patch.object(packer, "ZipContainerPacker", return_value=spec), \
             mock.patch.object(packer, "IOBigData", return_value=iobd), \
             mock.patch.object(packer.resources, "mem_manager", Lock), \
             mock.patch.object(packer, "FAST_MAX_BYTES", max_bytes), \
             mock.patch.object(packer, "CACHE", tempfile.gettempdir()):
            self.assertEqual(packer.ok("p/", "aux", fast=True, notes=notes)[0], "sid")
        return spec, locked[0], notes

    def test_stays_fast_with_room(self):
        spec, locked, notes = self._ok(ram_available=1 << 40)
        self.assertTrue(spec.fast)
        self.assertEqual(locked, packer.packing_memory_estimate(inline_len=self.IMAGE))
        self.assertEqual(notes, {})

    def test_falls_back_over_the_size_ceiling(self):
        spec, locked, notes = self._ok(ram_available=1 << 40, max_bytes=self.IMAGE - 1)
        self.assertFalse(spec.fast)
        self.assertEqual(locked, packer.packing_memory_estimate(self.SMALL, self.BLOCKS))
        self.assertIn("packer.FAST_MAX_BYTES", notes["fast_fallback"])

    def test_falls_back_without_the_ram(self):
        spec, locked, notes = self._ok(ram_available=1 << 30)  # 6 x 300 MB does not fit
        self.assertFalse(spec.fast)
        self.assertEqual(locked, packer.packing_memory_estimate(self.SMALL, self.BLOCKS))
        self.assertIn("RAM", notes["fast_fallback"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FallbackIsReportedTests(unittest.TestCase):
    """The worker hands the reason to pack_zip, which prints it and sets the stage."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="nodo packer fast test-")
        self.addCleanup(self._tmp.cleanup)
        patch_ = mock.patch.object(packer, "CACHE", self._tmp.name + os.sep)
        patch_.start()
        self.addCleanup(patch_.stop)

    def test_worker_writes_the_reason(self):
        import json

        def zipfile_ok(zip, fast=False, notes=None):
            notes["fast_fallback"] = "too big"
            return "sid", None, "/dir"

        result = os.path.join(self._tmp.name, "result.json")
        with mock.patch.object(packer, "zipfile_ok", zipfile_ok), \
             mock.patch.object(packer.sys, "argv", ["w", "--worker", "z.zip", result, "--fast"]):
            packer._worker_main()
        with open(result) as f:
            self.assertEqual(json.load(f)["fast_fallback"], "too big")

    def test_pack_zip_reports_it(self):
        import json

        def run(cmd, cwd=None):
            with open(cmd[cmd.index("--worker") + 2], "w") as f:
                json.dump({"error": "stop here", "fast_fallback": "too big"}, f)
            return mock.Mock(returncode=0)

        from src.utils import pack_registry
        with mock.patch("subprocess.run", run), \
             mock.patch.object(pack_registry, "stage") as stage, \
             mock.patch("builtins.print") as printed:
            list(packer.pack_zip(zip=os.path.join(self._tmp.name, "s.zip"), fast=True))
        self.assertIn("too big", stage.call_args.args[0])
        self.assertIn("fell back", stage.call_args.args[0])
        self.assertTrue(any("too big" in str(c) for c in printed.call_args_list))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OptimizeReplacesFastBlockTests(unittest.TestCase):
    """A normal pack replaces a stored fast filesystem block (same id, other form)."""

    def setUp(self):
        import shutil
        from bee_rpc.utils import modify_env
        self.root = tempfile.mkdtemp(prefix="nodo-fast-replace-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.blocks = os.path.join(self.root, "blocks")
        os.makedirs(self.blocks)
        modify_env(cache_dir=self.root + os.sep, block_dir=self.blocks + os.sep)
        self.addCleanup(modify_env, cache_dir=packer.CACHE, block_dir=packer.BLOCKDIR)
        patch_ = mock.patch.object(packer, "BLOCKDIR", self.blocks + os.sep)
        patch_.start()
        self.addCleanup(patch_.stop)
        t = packer.MIN_BUFFER_BLOCK_SIZE
        self.files = [("small", b"s" * 300), ("big", os.urandom(t * 3)), ("big2", os.urandom(t * 2))]

    def _pack(self, fast):
        from bee_rpc import block_builder
        from bee_rpc.utils import block_pointer, hash_types_for_packing
        from protos import celaut_pb2 as celaut
        tree, pointed = celaut.Service.Container.Filesystem(), []
        for name, data in self.files:
            path = os.path.join(self.root, name)
            with open(path, "wb") as f:
                f.write(data)
            branch = tree.branch.add()
            branch.name = name
            if packer._is_inline(len(data), fast):
                branch.file = data
            else:
                block_hash, _ = block_builder.create_block(file_path=path, copy=True)
                branch.file = block_pointer(block_id=block_hash, omit_types=True).SerializeToString()
                pointed.append(block_hash)
        return packer._install_as_block(*block_builder.build_multiblock(
            pf_object_with_block_pointers=tree, blocks=pointed, inherited=hash_types_for_packing()))

    def _stored(self, block_id):
        from bee_rpc import client as grpcbb
        directory = os.path.join(self.blocks, block_id.hex())
        wbp = os.path.getsize(os.path.join(directory, "wbp.bin"))
        content = b"".join(grpcbb.read_block(block_id=block_id.hex(), ignore_blocks=True))
        return wbp, packer._points_at_blocks(directory), content

    def _fast_then_optimize(self):
        fast_id = self._pack(fast=True)
        fast_wbp, fast_points, fast_content = self._stored(fast_id)
        self.assertFalse(fast_points)
        self.assertGreater(fast_wbp, sum(len(d) for _, d in self.files))
        opt_id = self._pack(fast=False)
        self.assertEqual(opt_id, fast_id)
        wbp, points, content = self._stored(opt_id)
        self.assertTrue(points)
        self.assertLess(wbp, packer.MIN_BUFFER_BLOCK_SIZE)  # only the small file
        self.assertEqual(content, fast_content)
        leftovers = [n for n in os.listdir(self.blocks) if ".tmp-" in n or ".old-" in n]
        self.assertEqual(leftovers, [])

    def test_fast_then_optimize_replaces_the_inlined_form(self):
        self._fast_then_optimize()

    def test_without_an_atomic_exchange(self):
        with mock.patch.object(packer, "_exchange", return_value=False):
            self._fast_then_optimize()

    def test_fast_after_optimize_keeps_the_block_form(self):
        block_id = self._pack(fast=False)
        before = self._stored(block_id)
        self._pack(fast=True)
        self.assertEqual(self._stored(block_id), before)


if __name__ == "__main__":
    unittest.main()

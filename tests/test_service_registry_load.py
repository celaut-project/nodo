"""Unit tests for the registry loader's failure discrimination.

``read_service_from_disk`` answers ``None`` for three unrelated situations: the
spec was never stored here, the memory lock timed out, and the read failed. A
caller that authorizes on what a spec declares cannot act on that single answer,
so ``load_service_from_disk`` keeps them apart (#269). These tests pin which
failure maps to which exception, and that the ``Optional`` wrapper still absorbs
all of them.

``src/utils/utils.py`` is loaded from its file under a private module name, with
the modules the loader does not exercise stubbed out and every stub undone
afterwards: importing it by name would cache a copy of the node's utils built
against those stubs for the whole test session. The node logger is among the
stubs because importing it creates ``STORAGE`` on disk, which a test host is not
entitled to do.
"""
import importlib.util
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

try:
    from protos import celaut_pb2 as celaut
    from src.utils.registry_errors import ServiceNotInRegistry, ServiceSpecUnavailable

    def _load_utils_module():
        saved_modules = {}
        saved_attrs = {}

        def stub(name, **attrs):
            saved_modules.setdefault(name, sys.modules.get(name))
            module = types.ModuleType(name)
            for key, value in attrs.items():
                setattr(module, key, value)
            sys.modules[name] = module
            if "." in name:
                # `from pkg import sub` reads the attribute off the package, so a
                # sys.modules entry alone is not enough to shadow a submodule.
                parent, leaf = name.rsplit(".", 1)
                if parent in sys.modules:
                    saved_attrs.setdefault(
                        (parent, leaf), getattr(sys.modules[parent], leaf, None)
                    )
                    setattr(sys.modules[parent], leaf, module)
            return module

        import src.utils  # noqa: F401 - the package must exist before it is patched

        # Importing the node logger creates STORAGE; the loader only calls LOGGER.
        stub("src.utils.logger", LOGGER=lambda message: None)

        # Out of bee_rpc the loader needs one filename constant and a type marker,
        # and out of netifaces nothing at all -- a host without either can still
        # exercise the registry logic.
        try:
            import bee_rpc  # noqa: F401
        except Exception:
            bee_rpc = stub("bee_rpc")
            bee_rpc.client = stub("bee_rpc.client", Dir=type("Dir", (), {}))
            bee_rpc.block_driver = stub(
                "bee_rpc.block_driver",
                WITHOUT_BLOCK_POINTERS_FILE_NAME="without_block_pointers",
            )
        try:
            import netifaces  # noqa: F401
        except Exception:
            stub("netifaces")

        try:
            path = Path(__file__).resolve().parents[1] / "src" / "utils" / "utils.py"
            spec = importlib.util.spec_from_file_location("node_utils_under_test", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            for (parent, leaf), previous in saved_attrs.items():
                if previous is None:
                    delattr(sys.modules[parent], leaf)
                else:
                    setattr(sys.modules[parent], leaf, previous)
            for name, previous in saved_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

    u = _load_utils_module()
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = exc
    celaut = None
    u = None
    ServiceNotInRegistry = ServiceSpecUnavailable = None


@contextmanager
def _no_lock(length, timeout=None):
    yield None


@contextmanager
def _lock_times_out(length, timeout=None):
    raise TimeoutError(f"waited {timeout}s for {length} bytes")
    yield None  # pragma: no cover - unreachable, keeps this a generator


@unittest.skipIf(IMPORT_ERROR, f"imports unavailable: {IMPORT_ERROR}")
class LoadServiceFromDiskTest(unittest.TestCase):
    def setUp(self):
        self._registry = tempfile.TemporaryDirectory()
        self.addCleanup(self._registry.cleanup)
        saved = (u.REGISTRY, u.mem_manager)
        self.addCleanup(lambda: setattr(u, "REGISTRY", saved[0]))
        self.addCleanup(lambda: setattr(u, "mem_manager", saved[1]))
        u.REGISTRY = self._registry.name
        u.mem_manager = _no_lock

    def _store(self, service_hash, service):
        Path(self._registry.name, service_hash).write_bytes(service.SerializeToString())

    def test_a_stored_spec_is_returned(self):
        service = celaut.Service()
        service.network.append(celaut.Service.Network(tags=["example.com"]))
        self._store("abcd", service)

        loaded = u.load_service_from_disk(service_hash="abcd")
        self.assertEqual([list(n.tags) for n in loaded.network], [["example.com"]])

    def test_absence_is_its_own_failure(self):
        with self.assertRaises(ServiceNotInRegistry):
            u.load_service_from_disk(service_hash="never-stored")

    def test_a_memory_lock_timeout_is_not_absence(self):
        # The load-dependent branch: under memory pressure the spec is here and
        # simply could not be read yet.
        self._store("abcd", celaut.Service())
        u.mem_manager = _lock_times_out
        with self.assertRaises(ServiceSpecUnavailable):
            u.load_service_from_disk(service_hash="abcd")

    def test_an_unreadable_block_directory_is_not_absence(self):
        # A directory entry means a blocks-based spec, whose payload lives in a
        # named file inside it. The entry exists, so this is a broken read rather
        # than a spec the node never stored.
        Path(self._registry.name, "abcd").mkdir()
        with self.assertRaises(ServiceSpecUnavailable):
            u.load_service_from_disk(service_hash="abcd")

    def test_the_optional_wrapper_still_absorbs_every_failure(self):
        # Callers that only report a miss (inspect, cost estimation) keep the old
        # signature and the old answer.
        self.assertIsNone(u.read_service_from_disk(service_hash="never-stored"))

        self._store("abcd", celaut.Service())
        u.mem_manager = _lock_times_out
        self.assertIsNone(u.read_service_from_disk(service_hash="abcd"))

        u.mem_manager = _no_lock
        self.assertIsNotNone(u.read_service_from_disk(service_hash="abcd"))


if __name__ == "__main__":
    unittest.main()

"""`execute` picks the backend by the service's architecture, once.

The same :func:`select_virtualizer` call the launcher makes to fill the
``virtualizer`` column, so the row and the running guest can never disagree about
who booted it.
"""
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers import interface as vm_interface
    from src.virtualizers import registry
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    vm_interface = None  # type: ignore[assignment]
    registry = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ExecuteSelectsTheBackendByArchTests(unittest.TestCase):
    def setUp(self):
        self.backends = {}
        for name in (registry.CH, registry.QEMU):
            self.backends[name] = registry.Backend(
                name=name,
                family=registry.MICROVM,
                execute=MagicMock(return_value=("vm", "ip", None)),
                kill=MagicMock(),
                maintain=MagicMock(),
                hotplug=MagicMock(),
            )
        patcher = patch.dict(vm_interface.BACKENDS, self.backends)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _execute(self, selected):
        with patch.object(vm_interface, "select_virtualizer", return_value=selected):
            return vm_interface.execute(
                assigment_ports=None,
                by_local=True,
                service_id="svc",
                service=celaut.Service(),
                config=None,
                system_resources=celaut.Service.Container.Resources(),
                father_id="",
            )

    def test_a_native_service_takes_the_ch_path(self):
        self._execute(registry.CH)
        self.backends[registry.CH].execute.assert_called_once()
        self.backends[registry.QEMU].execute.assert_not_called()

    def test_a_foreign_arch_service_takes_the_qemu_path(self):
        self._execute(registry.QEMU)
        self.backends[registry.QEMU].execute.assert_called_once()
        self.backends[registry.CH].execute.assert_not_called()

    def test_the_whole_declared_range_reaches_the_backend(self):
        # Which end of it a backend acts on at launch is the backend's business:
        # CH boots at `at_init` and raises the cgroup, QEMU has to reserve
        # `at_most` up front because `-m` is fixed for the life of the process.
        self._execute(registry.QEMU)
        _, kwargs = self.backends[registry.QEMU].execute.call_args
        self.assertIn("system_resources", kwargs)


if __name__ == "__main__":
    unittest.main()

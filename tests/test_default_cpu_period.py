"""A Sysresources with a cpu_quota and no cpu_period uses the default period, 100000.

celaut.proto says so (Sysresources.cpu_period), and admission already read it that
way. The display of `nodo resources` said "not stated", and billing priced the CPU
at 0 vCPUs.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.commands import resources as command
    from src.utils.cost_functions.execution_cost import CPU, requested_units
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DefaultCpuPeriodTests(unittest.TestCase):
    def test_nodo_resources_shows_the_cores_without_a_period(self):
        entry = celaut.ArchitectureResources(resources=celaut.Sysresources(cpu_quota=200_000))
        self.assertEqual(command._cores(entry), 2.0)

    def test_nodo_resources_still_uses_a_given_period(self):
        entry = celaut.ArchitectureResources(
            resources=celaut.Sysresources(cpu_quota=200_000, cpu_period=50_000)
        )
        self.assertEqual(command._cores(entry), 4.0)

    def test_no_quota_is_not_stated(self):
        self.assertIsNone(command._cores(celaut.ArchitectureResources()))

    def test_billing_counts_the_cpu_without_a_period(self):
        units = requested_units(celaut.Sysresources(cpu_quota=200_000))
        self.assertEqual(units[CPU], 2)


if __name__ == "__main__":
    unittest.main()

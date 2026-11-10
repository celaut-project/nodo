"""`Service` and `Configuration` do not change for the closing policy (issue #344).

The closing policy belongs to the node, not to the service: a new field in `Service` would
change the hash that is the id of a service. This test pins the fields as they were.
"""
import unittest

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

SERVICE = [("prose", 1), ("container", 2), ("api", 3), ("network", 4),
           ("possible_environment_workload", 5), ("initial_environment_workload", 6)]
CONFIGURATION = [("environment_variables", 1), ("spec_slot", 2), ("initial_mu", 3)]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServiceSpecUnchangedTests(unittest.TestCase):
    def _fields(self, message):
        return [(f.name, f.number) for f in message.DESCRIPTOR.fields]

    def test_service_has_no_new_field(self):
        self.assertEqual(self._fields(celaut_pb2.Service), SERVICE)

    def test_configuration_has_no_new_field(self):
        self.assertEqual(self._fields(celaut_pb2.Configuration), CONFIGURATION)

    def test_the_bytes_of_a_fixed_service_are_the_same(self):
        service = celaut_pb2.Service(prose="fixed")
        service.container.architecture.tags.append("linux/amd64")
        self.assertEqual(service.SerializeToString(deterministic=True).hex(),
                         "0a056669786564120f0a0d0a0b6c696e75782f616d643634")


if __name__ == "__main__":
    unittest.main()

"""Pins the ResourceAvailability message and GetResourceAvailability RPC added
to celaut.proto for the possible_environment_workload admission check
(src/utils/cost_functions/workload_admission.py). Regenerating protos/*_pb2.py
from a stale celaut.proto, or dropping the RPC from the Gateway service,
breaks silently until a real peer round-trip is attempted -- this catches it
at collection time instead.
"""
import unittest

from protos import celaut_pb2 as celaut


class ResourceAvailabilityMessageTests(unittest.TestCase):
    def test_fields(self):
        msg = celaut.ResourceAvailability(can_execute=True, reason="")
        self.assertTrue(msg.can_execute)
        self.assertEqual(msg.reason, "")

    def test_default_can_execute_is_false(self):
        self.assertFalse(celaut.ResourceAvailability().can_execute)


class GatewayServiceSpecTests(unittest.TestCase):
    def test_get_resource_availability_rpc_is_declared(self):
        method_names = [
            m.name for m in celaut.DESCRIPTOR.services_by_name["Gateway"].methods
        ]
        self.assertIn("GetResourceAvailability", method_names)
        # Grouped next to the RPC it complements in the spec.
        self.assertLess(
            method_names.index("GetServiceEstimatedCost"),
            method_names.index("GetResourceAvailability"),
        )


if __name__ == "__main__":
    unittest.main()

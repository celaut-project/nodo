"""Routing a service with devices to peers that can give them (#536, phase P7)."""
import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.devices import spec  # noqa: E402
from src.utils.cost_functions import architecture_resources as ar  # noqa: E402
from src.utils.cost_functions import workload_admission as wa  # noqa: E402
from src.utils.hashing import SHA256_ID  # noqa: E402

TAG = spec.make_tag("10de", "2204", SHA256_ID, "a" * 64)
OTHER = spec.make_tag("1002", "744c", SHA256_ID, "a" * 64)


def _device(tag=TAG):
    d = celaut.Device()
    d.architecture.tags.append(tag)
    return d


class PreFilterTests(unittest.TestCase):
    def _skip(self, announced, asked):
        with patch.object(ar, "stored_announcement", return_value=[]), \
                patch.object(ar, "stored_activity_window", return_value=None), \
                patch.object(ar, "stored_devices", return_value=announced):
            return ar.should_skip_peer("p", "linux/amd64", celaut.Sysresources(), "X", devices=asked)

    def test_no_devices_asked_changes_nothing(self):
        self.assertFalse(self._skip([], []))

    def test_a_peer_that_announced_no_devices_is_skipped(self):
        self.assertTrue(self._skip([], [_device()]))

    def test_matched_by_tag_and_count(self):
        self.assertFalse(self._skip([_device()], [_device()]))
        self.assertTrue(self._skip([_device(OTHER)], [_device()]))
        self.assertTrue(self._skip([_device()], [_device(), _device()]))
        self.assertFalse(self._skip([_device(), _device()], [_device(), _device()]))


class WorkloadTests(unittest.TestCase):
    def _workload(self):
        w = celaut.Service.Workload(count=1)
        c = w.dependency.service.container
        c.resources.at_init.mem_limit = 1 << 30
        c.resources.at_most.mem_limit = 1 << 30
        c.devices.add().architecture.tags.append(TAG)
        return w

    def test_a_child_with_devices_asks_peers_with_the_devices(self):
        w = self._workload()
        self.assertEqual(len(wa.group_devices(w)), 1)
        asked = []
        with patch.object(wa, "_local_resource_availability", return_value={"can_execute": False}), \
                patch("src.utils.cost_functions.architecture_resources.executes_locally", return_value=True), \
                patch("src.utils.utils.peers_id_iterator", return_value=iter(["p1", "p2"])), \
                patch("src.utils.cost_functions.architecture_resources.should_skip_peer",
                      side_effect=lambda peer, *a, **k: peer == "p1") as skip, \
                patch.object(wa, "check_resource_availability_on_peer",
                             side_effect=lambda peer, request: asked.append((peer, request)) or True):
            self.assertTrue(wa._workload_group_is_satisfiable(
                wa.group_request(w), None, wa.group_devices(w)))
        self.assertEqual(skip.call_args_list[0].kwargs["devices"][0].architecture.tags[0], TAG)
        [(peer, request)] = asked
        self.assertEqual(peer, "p2")
        self.assertIsInstance(request, celaut.ResourceAvailabilityRequest)
        self.assertEqual(list(request.devices[0].architecture.tags), [TAG])

    def test_this_node_answers_with_its_devices(self):
        with patch("src.devices.admission.request_availability",
                   return_value={"can_execute": True}) as local:
            self.assertTrue(wa._local_resource_availability(
                celaut.ArchitectureResources(), [_device()])["can_execute"])
        local.assert_called_once()

    def test_a_group_that_asks_only_devices_is_checked(self):
        w = celaut.Service.Workload(count=1)
        w.dependency.service.container.devices.add().architecture.tags.append(TAG)
        group = celaut.Service.Group()
        group.workloads.add().CopyFrom(w)
        with patch.object(wa, "_workload_group_is_satisfiable", return_value=False) as probe:
            failures = wa._unsatisfiable_workloads(group, "g", None, stop_at_first=True)
        probe.assert_called_once()
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()

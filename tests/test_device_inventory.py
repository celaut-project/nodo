"""The devices that a node offers (#536, phase P3): config, host checks, kernels,
quarantine, the assignment table, GetResourceAvailability and ``nodo devices``."""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from protos import celaut_pb2 as celaut
from src.commands import devices as devices_command
from src.database.migrate import TABLES
from src.devices import admission, assignments, host, inventory, spec
from src.utils.config_validation import ConfigValidationError, validate_devices_config
from src.utils.hashing import SHA256_ID, SHA3_256_ID
from src.virtualizers.microvm import kernel_store, paths

ADDR = "0000:0b:00.0"
CHIP = "pci:10de:2204"
KERNEL = b"device kernel"
K = hashlib.sha256(KERNEL).hexdigest()
K3 = hashlib.sha3_256(KERNEL).hexdigest()


class _Host:
    """A fake sysfs + /dev + node config with one GPU bound to vfio-pci."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sysfs = self.root / "sys"
        self.dev = self.root / "dev"
        self.main = self.root / "nodo"
        self.kernel = self.main / "vmlinuz"
        self.main.mkdir()
        self.kernel.write_bytes(KERNEL)
        (self.sysfs / "bus" / "pci" / "drivers" / "vfio-pci").mkdir(parents=True)
        (self.sysfs / "bus" / "pci" / "drivers" / "nvidia").mkdir(parents=True)
        (self.dev / "vfio").mkdir(parents=True)
        (self.dev / "vfio" / "vfio").write_text("")
        self.add_device(ADDR, "12")
        self.config = {
            "main.MAIN_DIR": str(self.main),
            "main.STORAGE": str(self.root / "storage"),
            "virtualizers.ch.KERNEL_PATHS": {"linux/amd64": str(self.kernel)},
            "virtualizers.ch.KERNEL_ABI_ALIASES": {},
            "devices": {
                "ENABLED": True,
                "ALLOW": [ADDR],
                "KERNELS": [],
                "BY_CHIP": {CHIP: {"mu_per_second": 7, "mem_limit": 24 << 30,
                                   "benchmark": {"fp32_flops": 35}, "watts": 350}},
            },
        }
        self.assigned = {}
        self.patches = [
            patch.object(kernel_store.env_manager, "get", side_effect=self.get),
            patch.object(paths.env_manager, "get", side_effect=self.get),
            patch.object(inventory.env_manager, "get", side_effect=self.get),
            patch("src.utils.arch_guard.host_arch_tag", return_value="linux/amd64"),
            patch.object(assignments, "assigned", side_effect=lambda: dict(self.assigned)),
            patch.object(inventory, "_daemon_ids", return_value=(0, ())),
        ]

    def add_device(self, address, group, driver="vfio-pci", vendor="0x10de", device="0x2204",
                   pci_class="0x030000", reset_method="flr"):
        d = self.sysfs / "bus" / "pci" / "devices" / address
        d.mkdir(parents=True)
        (d / "vendor").write_text(vendor + "\n")
        (d / "device").write_text(device + "\n")
        (d / "class").write_text(pci_class + "\n")
        (d / "reset_method").write_text(reset_method + "\n")
        (d / "reset").write_text("")
        (d / "config").write_bytes(b"\xde\x10" + b"\0" * 62)
        if driver:
            os.symlink(str(self.sysfs / "bus" / "pci" / "drivers" / driver), str(d / "driver"))
        g = self.sysfs / "kernel" / "iommu_groups" / group
        (g / "devices").mkdir(parents=True, exist_ok=True)
        (g / "devices" / address).write_text("")
        os.symlink(str(g), str(d / "iommu_group"))
        (self.dev / "vfio" / group).write_text("")

    def get(self, key, default=None):
        if key.startswith("devices.") or key == "devices":
            value = self.config["devices"]
            for part in key.split(".")[1:]:
                value = value.get(part) if isinstance(value, dict) else None
            return default if value is None else value
        return self.config.get(key, default)

    def states(self):
        return inventory.states(sysfs=str(self.sysfs), dev_root=str(self.dev))

    def free(self):
        return inventory.free_devices(sysfs=str(self.sysfs), dev_root=str(self.dev))

    def offered(self):
        return inventory.node_devices(sysfs=str(self.sysfs), dev_root=str(self.dev))

    def __enter__(self):
        kernel_store._HASH_CACHE.clear()
        inventory._DIGEST_CACHE.clear()
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


def _asked(tag, mem=None):
    d = celaut.Device()
    d.architecture.tags.append(tag)
    if mem is not None:
        d.resources.at_init.mem_limit = mem
    return d


class InventoryTests(unittest.TestCase):
    def test_disabled_offers_nothing(self):
        with _Host() as h:
            h.config["devices"]["ENABLED"] = False
            self.assertEqual(h.offered(), [])
            self.assertEqual(h.free(), [])

    def test_a_bound_priced_device_is_one_entry_per_kernel_with_both_hash_tags(self):
        with _Host() as h:
            [entry] = h.offered()
            self.assertEqual(entry.address, ADDR)
            self.assertEqual(entry.chip, CHIP)
            self.assertEqual(entry.kernel_hash, K)
            self.assertEqual(entry.tags, {
                spec.make_tag("10de", "2204", SHA256_ID, K),
                spec.make_tag("10de", "2204", SHA3_256_ID, K3),
            })
            for tag in entry.tags:
                self.assertEqual(spec.parse_tag(tag).chip, CHIP)
            proto = entry.to_proto()
            self.assertEqual(proto.resources.at_most.mem_limit, 24 << 30)
            self.assertEqual(spec.device_reason(proto), None)

    def test_a_device_without_a_price_is_not_offered(self):
        with _Host() as h:
            h.config["devices"]["BY_CHIP"] = {}
            [st] = h.states()
            self.assertEqual(st.state, "no price")
            self.assertEqual(h.offered(), [])

    def test_a_device_on_a_host_driver_is_not_offered(self):
        with _Host() as h:
            h.add_device("0000:0c:00.0", "13", driver="nvidia")
            h.config["devices"]["ALLOW"] = ["0c:00.0"]
            [st] = h.states()
            self.assertEqual(st.state, "unusable")
            self.assertTrue(any("not vfio-pci" in f.message for f in st.report.findings))

    def test_an_incomplete_iommu_group_is_not_offered(self):
        with _Host() as h:
            # The audio function of the same card, still on a host driver, in group 12.
            h.add_device("0000:0b:00.1", "12", driver="nvidia", pci_class="0x040300")
            [st] = h.states()
            self.assertEqual(st.state, "unusable")

    def test_quarantine_hides_a_device_until_cleared(self):
        with _Host() as h:
            inventory.quarantine(ADDR, "FLR failed")
            self.assertEqual(h.states()[0].state, "quarantined")
            self.assertEqual(h.offered(), [])
            self.assertEqual(inventory.clear_quarantine(ADDR), [ADDR])
            self.assertEqual(len(h.offered()), 1)

    def test_an_assigned_device_is_announced_but_not_free(self):
        with _Host() as h:
            h.assigned = {ADDR: "instance-1"}
            self.assertEqual(len(h.offered()), 1)
            self.assertEqual(h.free(), [])
            self.assertEqual(h.states()[0].state, "assigned")

    def test_a_configured_kernel_that_is_not_on_the_node_is_a_problem(self):
        with _Host() as h:
            h.config["devices"]["KERNELS"] = ["ab" * 32]
            kernels, problems = inventory.offered_kernels()
            self.assertEqual(kernels, [])
            self.assertIn("ab" * 32, problems[0])
            self.assertEqual(h.offered(), [])

    def test_an_abi_alias_adds_the_older_hash_as_a_tag(self):
        with _Host() as h:
            old = "cd" * 32
            h.config["virtualizers.ch.KERNEL_ABI_ALIASES"] = {K: [old]}
            [entry] = h.offered()
            self.assertIn(spec.make_tag("10de", "2204", SHA256_ID, old), entry.tags)


class AvailabilityTests(unittest.TestCase):
    def _ask(self, h, devices, main=None):
        free = h.free()
        with patch.object(inventory, "free_devices", return_value=free), patch(
            "src.utils.cost_functions.resource_availability.get_architecture_availability",
            return_value={"can_execute": True},
        ):
            return admission.request_availability(main or celaut.ArchitectureResources(), devices)

    def test_a_free_matching_device_is_a_yes(self):
        with _Host() as h:
            tag = spec.make_tag("10de", "2204", SHA3_256_ID, K3)
            self.assertEqual(self._ask(h, [_asked(tag)]), {"can_execute": True})

    def test_two_devices_on_a_node_with_one_is_a_no(self):
        with _Host() as h:
            tag = spec.make_tag("10de", "2204", SHA256_ID, K)
            answer = self._ask(h, [_asked(tag), _asked(tag)])
            self.assertFalse(answer["can_execute"])

    def test_more_memory_than_the_device_has_is_a_no(self):
        with _Host() as h:
            tag = spec.make_tag("10de", "2204", SHA256_ID, K)
            answer = self._ask(h, [_asked(tag, mem=48 << 30)])
            self.assertFalse(answer["can_execute"])
            self.assertIn("no free device", answer["reason"])

    def test_a_held_device_is_a_no(self):
        with _Host() as h:
            h.assigned = {ADDR: "other"}
            tag = spec.make_tag("10de", "2204", SHA256_ID, K)
            self.assertFalse(self._ask(h, [_asked(tag)])["can_execute"])

    def test_a_foreign_main_architecture_is_a_no(self):
        with _Host() as h:
            tag = spec.make_tag("10de", "2204", SHA256_ID, K)
            main = celaut.ArchitectureResources()
            main.architecture.tags.append("linux/arm64")
            answer = self._ask(h, [_asked(tag)], main)
            self.assertEqual(answer["reason"], admission.NATIVE_ONLY_REASON)

    def test_disabled_is_a_no(self):
        with _Host() as h:
            h.config["devices"]["ENABLED"] = False
            tag = spec.make_tag("10de", "2204", SHA256_ID, K)
            self.assertEqual(self._ask(h, [_asked(tag)])["reason"], admission.NOT_ATTACHING_REASON)


class _Db:
    """The assignment table in an in-memory database, behind the SQLConnection calls."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(TABLES["device_assignments"])

    def _execute(self, query, params=()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        self.conn.commit()
        return cur

    def _execute2(self, queries):
        cur = self.conn.cursor()
        try:
            for query, params in queries:
                cur.execute(query, params)
            self.conn.commit()
        except sqlite3.Error:
            self.conn.rollback()
            raise
        return cur


class AssignmentTableTests(unittest.TestCase):
    def entry(self, address):
        return spec.NodeDevice(address=address, chip=CHIP, kernel_hash=K, tags=frozenset())

    def test_one_address_is_held_by_one_instance_and_all_or_nothing(self):
        db = _Db()
        with patch.object(assignments, "_db", return_value=db):
            self.assertTrue(assignments.reserve("a", [self.entry("x"), self.entry("y")]))
            # "z" is free but "y" is not: nothing is reserved.
            self.assertFalse(assignments.reserve("b", [self.entry("z"), self.entry("y")]))
            self.assertEqual(assignments.assigned(), {"x": "a", "y": "a"})
            self.assertEqual(sorted(assignments.release("a")), ["x", "y"])
            self.assertTrue(assignments.reserve("b", [self.entry("y")]))
            self.assertEqual(assignments.assigned(), {"y": "b"})


class ConfigTests(unittest.TestCase):
    def test_valid_and_absent(self):
        validate_devices_config({})
        validate_devices_config({"devices": {
            "ENABLED": True, "ALLOW": ["0b:00.0"], "KERNELS": ["a" * 64],
            "BY_CHIP": {CHIP: {"mu_per_second": 1, "mem_limit": 2, "watts": 3.5,
                               "benchmark": {"fp32_flops": 4}}},
        }})

    def test_malformed_is_refused(self):
        for bad in (
            {"ENABLED": "yes"},
            {"ALLOW": ["gpu0"]},
            {"ALLOW": ["0b:00.0", "0000:0b:00.0"]},
            {"KERNELS": ["short"]},
            {"BY_CHIP": {"10de:2204": {}}},
            {"BY_CHIP": {CHIP: {"mu_per_second": -1}}},
            {"BY_CHIP": {CHIP: {"price": 1}}},
            {"BY_CHIP": {CHIP: {"benchmark": {"x": -1}}}},
            {"BY_ALL": {}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigValidationError):
                validate_devices_config({"devices": bad})


class CommandTests(unittest.TestCase):
    def test_json_report_and_clear(self):
        with _Host() as h:
            rep = inventory.report(sysfs=str(h.sysfs), dev_root=str(h.dev))
            with patch.object(inventory, "report", return_value=rep):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertTrue(devices_command.devices(["--json"]))
            data = json.loads(out.getvalue().strip().splitlines()[-1])
            self.assertTrue(data["enabled"])
            [d] = data["devices"]
            self.assertEqual((d["address"], d["chip"], d["state"]), (ADDR, CHIP, "free"))
            self.assertEqual(d["kernels"], [K])
            inventory.quarantine(ADDR, "AER")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertTrue(devices_command.devices(["clear", "0b:00.0"]))
            self.assertIn(ADDR, out.getvalue())
            self.assertEqual(inventory.quarantined(), {})


if __name__ == "__main__":
    unittest.main()

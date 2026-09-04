"""The interface routes; it does not implement, and it does not import a backend.

Every lifecycle call resolves through ``src.virtualizers.registry``: per instance
by the ``virtualizer`` column the launcher recorded, and per family for the calls
that are the family's (the build cache, the janitor's sweep). Holding that down
here is what keeps the node from reaching past this module into one backend's
implementation, which is how the janitor came to judge QEMU guests with CH's
liveness test (#295).
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


def _fake_backend(name, family=None):
    return registry.Backend(
        name=name,
        family=family or registry.MICROVM,
        execute=MagicMock(return_value=("vm", "ip", None)),
        kill=MagicMock(return_value=True),
        maintain=MagicMock(),
        hotplug=MagicMock(return_value=True),
    )


def _fake_family(name=None):
    return registry.Family(
        name=name or registry.MICROVM,
        build=MagicMock(return_value="svc"),
        is_built=MagicMock(return_value=True),
        remove_built=MagicMock(return_value=42),
        built_rootfs_size_bytes=MagicMock(return_value=1024),
        billable_resources=MagicMock(),
        sweep_orphans=MagicMock(),
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PerInstanceRoutingTests(unittest.TestCase):
    """kill/maintain/hotplug go to whichever backend the row names."""

    def setUp(self):
        self.ch = _fake_backend(registry.CH)
        self.qemu = _fake_backend(registry.QEMU)
        patcher = patch.dict(
            vm_interface.BACKENDS,
            {registry.CH: self.ch, registry.QEMU: self.qemu},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _recorded(self, value):
        return patch.object(
            vm_interface.sc, "get_internal_virtualizer", return_value=value
        )

    def test_kill_routes_to_the_recorded_backend(self):
        with self._recorded("qemu"):
            self.assertTrue(vm_interface.kill(vmachine_id="vm-q"))
        self.qemu.kill.assert_called_once_with(vmachine_id="vm-q")
        self.ch.kill.assert_not_called()

        with self._recorded("ch"):
            self.assertTrue(vm_interface.kill(vmachine_id="vm-c"))
        self.ch.kill.assert_called_once_with(vmachine_id="vm-c")

    def test_a_row_naming_nothing_recognizable_still_gets_torn_down(self):
        """The one place a default is right: this decides *who acts*.

        Refusing to act on a row whose column is empty or misspelled would leave
        a running guest nobody ever kills. The readers that judge a *guest* -- the
        janitor, the health check -- read the recorded facts instead and refuse to
        guess.
        """
        for recorded in (None, "", "docker"):
            with self.subTest(recorded=recorded), self._recorded(recorded):
                self.ch.kill.reset_mock()
                self.assertTrue(vm_interface.kill(vmachine_id="vm-u"))
                self.ch.kill.assert_called_once_with(vmachine_id="vm-u")

    def test_a_database_error_does_not_stop_a_teardown(self):
        with patch.object(
            vm_interface.sc, "get_internal_virtualizer", side_effect=RuntimeError("no db")
        ):
            self.assertTrue(vm_interface.kill(vmachine_id="vm-e"))
        self.ch.kill.assert_called_once_with(vmachine_id="vm-e")

    def test_maintain_routes_to_the_recorded_backend(self):
        callback = lambda vmachine_id: None
        with self._recorded("qemu"):
            vm_interface.maintain(
                vmachine_id="vm-3", debug_mode=True, remove_and_penalize=callback
            )
        self.qemu.maintain.assert_called_once_with(
            vmachine_id="vm-3", debug_mode=True, remove_and_penalize=callback
        )
        self.ch.maintain.assert_not_called()

    def test_hotplug_routes_to_the_recorded_backend(self):
        req = celaut.ModifyServiceSystemResourcesInput()
        with self._recorded("ch"):
            self.assertTrue(
                vm_interface.hotplug(vmachine_id="vm-1", system_requeriments_range=req)
            )
        self.ch.hotplug.assert_called_once_with(
            vmachine_id="vm-1", system_requeriments_range=req
        )
        self.qemu.hotplug.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PerFamilyRoutingTests(unittest.TestCase):
    """The build cache and the sweep belong to a family, not to one backend."""

    def setUp(self):
        self.family = _fake_family()
        patcher = patch.dict(vm_interface.FAMILIES, {registry.MICROVM: self.family})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_is_built_asks_the_family(self):
        self.assertTrue(vm_interface.is_built("svc-hash"))
        self.family.is_built.assert_called_once_with("svc-hash")

    def test_remove_built_service_asks_the_family(self):
        self.assertEqual(vm_interface.remove_built_service("svc-hash"), 42)
        self.family.remove_built.assert_called_once_with(service_id="svc-hash")

    def test_a_quote_for_a_built_service_carries_its_real_image_size(self):
        vm_interface.resolve_billable_resources(
            celaut.Sysresources(), service_hash="svc-hash"
        )
        self.family.built_rootfs_size_bytes.assert_called_once_with("svc-hash")
        _, kwargs = self.family.billable_resources.call_args
        self.assertEqual(kwargs["built_rootfs_size_bytes"], 1024)

    def test_a_quote_with_no_service_in_hand_does_not_touch_the_cache(self):
        vm_interface.resolve_billable_resources(celaut.Sysresources())
        self.family.built_rootfs_size_bytes.assert_not_called()
        _, kwargs = self.family.billable_resources.call_args
        self.assertIsNone(kwargs["built_rootfs_size_bytes"])

    def test_the_janitor_sweeps_every_family(self):
        other = _fake_family(name="remote")
        with patch.dict(vm_interface.FAMILIES, {"remote": other}):
            vm_interface.janitor_cleanup_orphans(debug_mode=True)
        self.family.sweep_orphans.assert_called_once_with(debug_mode=True)
        other.sweep_orphans.assert_called_once_with(debug_mode=True)

    def test_one_familys_failed_sweep_does_not_stop_the_others(self):
        # The maintenance tick calls this and then has instances to charge.
        other = _fake_family(name="remote")
        other.sweep_orphans.side_effect = OSError("boom")
        with patch.dict(vm_interface.FAMILIES, {"remote": other}):
            vm_interface.janitor_cleanup_orphans()
        self.family.sweep_orphans.assert_called_once()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class FirewallPassthroughTests(unittest.TestCase):
    def test_remove_firewall_rule_reaches_the_firewall_frontend(self):
        with patch.object(vm_interface, "vm_remove_rule", return_value=True) as vm_remove_rule:
            result = vm_interface.remove_firewall_rule(
                vmachine_id="vm-5",
                ip="1.1.1.1",
                port=53,
                protocol=vm_interface.TransportProtocol.UDP,
            )

        self.assertTrue(result)
        vm_remove_rule.assert_called_once_with(
            vmachine_id="vm-5",
            ip="1.1.1.1",
            port=53,
            protocol=vm_interface.TransportProtocol.UDP,
        )


if __name__ == "__main__":
    unittest.main()

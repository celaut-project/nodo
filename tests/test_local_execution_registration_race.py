"""An instance must be on record before its guest can call the node.

A guest starts running while `execute` is still waiting for its network and
applying firewall rules, and its first act is usually a call back to the node --
`node_controller`'s ModifyServiceSystemResources within a second of boot, in the
case that produced this file. Every such call is attributed to its caller by
source address, so an instance recorded only when the launch *finishes* is a
caller the node cannot name: it answered that first call with
`Error charging for the resource change of <ip>`, because the charge was where the
missing row surfaced first.

So the launcher hands the backend a callback, the backend calls it the instant the
guest becomes able to speak, and the instance definition -- which needs the address
the guest ended up with -- is filled in afterwards.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    import src.gateway.launcher.local_execution.local_execution as local_execute
    from src.utils.utils import to_amount
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    local_execute = None  # type: ignore[assignment]
    to_amount = None  # type: ignore[assignment]

VM_ID = "vm-1"
VM_IP = "192.168.200.38"
DECLARED_DISK = 5_000_000_000
RESOLVED = None  # built in setUp, once protos are known to be importable


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RegistrationOrderTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.config_values = {
            "network.ISOLATE_INTERNAL_CHILDREN": True,
            "network.CONSIDER_DEV_AS_INTERNAL": True,
            "network.DISABLE_EXPOSE_OUTSIDE": False,
            "network.FREE_PORTS_RANGE": [],
        }
        self.resolved = celaut.Sysresources(
            mem_limit=1_000_000_000,
            disk_space=DECLARED_DISK + 3584,  # the image the build actually handed it
            cpu_period=100000,
            cpu_quota=100000,
        )

    def _run(self, execute_side_effect, disk_space=DECLARED_DISK):
        config = celaut.Configuration(initial_mu=to_amount(1234))
        resources = celaut.Service.Container.Resources(
            at_init=celaut.Sysresources(mem_limit=1_000_000_000, disk_space=disk_space)
        )
        service = celaut.Service(
            api=celaut.Service.Api(
                slot=[
                    celaut.Service.Api.Slot(
                        port=5000,
                        transport=celaut.Service.Api.Protocol(tags=["tcp"]),
                    )
                ]
            )
        )

        def _record(name):
            def _fn(*_args, **kwargs):
                self.events.append((name, kwargs))
                return True
            return _fn

        with patch.object(
            local_execute.env_manager,
            "get",
            side_effect=lambda key, default=None: self.config_values.get(key, default),
        ), patch.object(
            local_execute, "get_configured_virtualizer", return_value="ch"
        ), patch.object(
            local_execute, "select_virtualizer", return_value="ch"
        ), patch.object(
            local_execute, "build", return_value="svc-hash"
        ), patch.object(
            local_execute, "reserve_instance_name", return_value="tidy-island"
        ), patch.object(
            local_execute.sc, "internal_instance_exists", return_value=False
        ), patch.object(
            local_execute, "resolve_slot_transport_protocols", return_value="tcp"
        ), patch.object(
            local_execute, "get_free_port", return_value=51000
        ), patch.object(
            local_execute.sc, "add_local_instance", side_effect=_record("add")
        ), patch.object(
            local_execute.sc, "set_local_instance_definition", side_effect=_record("definition")
        ), patch.object(
            local_execute.sc, "purge_internal", side_effect=_record("purge")
        ), patch.object(
            local_execute, "execute", side_effect=execute_side_effect
        ):
            return local_execute.local_execution(
                config=config,
                resources=resources,
                father_id="dev-1",
                father_ip="127.0.0.1",
                metadata=celaut.Metadata(),
                service=service,
                service_id="svc-hash",
                refund_container=[],
            )

    def _successful_execute(self, **kwargs):
        # What a backend does: register the instance the moment the guest starts,
        # then keep working (network readiness, firewall, DNAT) before returning.
        kwargs["register_instance"](VM_ID, VM_IP, self.resolved)
        self.events.append(("guest_calls_in", {}))
        return (VM_ID, VM_IP, self.resolved)

    def test_the_row_exists_before_the_guest_can_call_in(self):
        instance = self._run(self._successful_execute)

        order = [name for name, _ in self.events]
        self.assertEqual(order, ["add", "guest_calls_in", "definition"])
        self.assertEqual(instance.token, VM_ID)

    def test_the_row_carries_the_address_the_node_will_resolve_callers_by(self):
        self._run(self._successful_execute)

        add = dict(self.events[0][1])
        self.assertEqual(add["container_ip"], VM_IP)
        self.assertEqual(add["container_id"], VM_ID)
        self.assertEqual(add["name"], "tidy-island")
        self.assertEqual(add["balance_mu"], 1234)
        # Priced by what the virtualizer reserved, not by what the manifest asked
        # for -- the row is what the maintenance tick bills.
        self.assertEqual(add["mem_limit"], 1_000_000_000)
        self.assertEqual(add["cpu_quota"], 100000)
        self.assertEqual(add["disk_space"], DECLARED_DISK + 3584)

    def test_the_definition_is_stored_once_the_address_is_known(self):
        self._run(self._successful_execute)

        # It cannot be written with the row: the published URI slots are built from
        # the address the guest was given.
        self.assertIsNone(dict(self.events[0][1])["serialized_instance"])
        definition = dict(self.events[-1][1])
        self.assertEqual(definition["id"], VM_ID)
        stored = celaut.Instance()
        stored.ParseFromString(definition["serialized_instance"])
        self.assertEqual(stored.uri_slot[0].uri[0].ip, VM_IP)
        self.assertEqual(stored.uri_slot[0].uri[0].port, 5000)

    def test_a_launch_that_fails_after_registering_purges_the_row(self):
        def _fails_after_registering(**kwargs):
            kwargs["register_instance"](VM_ID, VM_IP, self.resolved)
            raise RuntimeError("guest network never came up")

        with self.assertRaises(RuntimeError):
            self._run(_fails_after_registering)

        self.assertEqual([name for name, _ in self.events], ["add", "purge"])
        self.assertEqual(dict(self.events[-1][1])["id"], VM_ID)

    def test_a_launch_that_fails_before_registering_purges_nothing(self):
        def _fails_early(**kwargs):
            raise RuntimeError("no bundle for this architecture")

        with self.assertRaises(RuntimeError):
            self._run(_fails_early)

        self.assertEqual(self.events, [])

    def test_a_manifest_with_no_disk_is_rejected_before_anything_boots(self):
        def _must_not_run(**kwargs):
            raise AssertionError("the VM was booted for a manifest that declares no disk")

        with self.assertRaises(Exception) as ctx:
            self._run(_must_not_run, disk_space=0)
        self.assertIn("Disk space is not specified", str(ctx.exception))
        self.assertEqual(self.events, [])

    def test_a_backend_that_does_not_register_is_still_recorded(self):
        # The callback is optional: a backend that ignores it must still end up
        # with a complete row, definition included.
        def _never_registers(**kwargs):
            return (VM_ID, VM_IP, self.resolved)

        self._run(_never_registers)

        self.assertEqual([name for name, _ in self.events], ["add", "definition"])
        self.assertEqual(dict(self.events[0][1])["container_ip"], VM_IP)


if __name__ == "__main__":
    unittest.main()

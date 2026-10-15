"""`network.EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE`: a node's own launches on the host interface.

Off, an instance a local dev client starts is internal and `execute` prints its
internal address (#437). On, it is also published on a port of the host interface and
that address is what gets advertised -- resolved from explicit config only, and never
loopback: when nothing usable resolves the instance still runs, internally, and the
reason is logged instead of `127.0.0.1` being handed out as if it worked.
"""
import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    import src.gateway.launcher.local_execution.local_execution as local_execute
    import src.commands.execute as execute_cmd
    from src.utils import host_interface
    from src.utils.host_interface import HOST_EXPOSURE_KEY, HostInterfaceUnresolved
    from src.utils.utils import to_amount
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

VM_ID = "vm-1"
VM_IP = "192.168.200.38"
AF_INET = 2
AF_INET6 = 30


def _fake_ifaddresses(table):
    def _lookup(interface):
        if interface not in table:
            raise ValueError("You must specify a valid interface name.")
        return table[interface]
    return _lookup


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveHostInterfaceIpTests(unittest.TestCase):
    def setUp(self):
        self.interfaces = {
            "eth0": {AF_INET: [{"addr": "172.28.10.5"}]},
            "lo": {AF_INET: [{"addr": "127.0.0.1"}], AF_INET6: [{"addr": "::1"}]},
            "llonly": {AF_INET: [{"addr": "169.254.3.4"}], AF_INET6: [{"addr": "fe80::1%llonly"}]},
            "v6": {AF_INET6: [{"addr": "fe80::2%v6"}, {"addr": "2001:db8::7"}]},
        }
        self.patches = [
            patch.object(host_interface.ni, "AF_INET", AF_INET),
            patch.object(host_interface.ni, "AF_INET6", AF_INET6),
            patch.object(host_interface.ni, "ifaddresses", side_effect=_fake_ifaddresses(self.interfaces)),
            patch.object(host_interface.ni, "gateways", return_value={}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_public_ip_wins(self):
        self.assertEqual(
            host_interface.resolve_host_interface_ip(public_ip="203.0.113.9", external_interface="eth0"),
            "203.0.113.9",
        )

    def test_external_interface_is_used_when_public_ip_is_empty(self):
        self.assertEqual(host_interface.resolve_host_interface_ip(external_interface="eth0"), "172.28.10.5")

    def test_link_local_is_skipped_for_a_global_ipv6(self):
        self.assertEqual(host_interface.resolve_host_interface_ip(external_interface="v6"), "2001:db8::7")

    def test_default_route_interface_is_the_last_source(self):
        with patch.object(host_interface.ni, "gateways", return_value={"default": {AF_INET: ("172.28.0.1", "eth0")}}):
            self.assertEqual(host_interface.resolve_host_interface_ip(), "172.28.10.5")

    def test_loopback_is_never_returned(self):
        for kwargs in (
            {"public_ip": "127.0.0.1"},
            {"public_ip": "localhost"},
            {"public_ip": "::1"},
            {"external_interface": "lo"},
            {"external_interface": "llonly"},
            {"external_interface": "missing0"},
            {},
        ):
            with self.subTest(**kwargs), self.assertRaises(HostInterfaceUnresolved):
                host_interface.resolve_host_interface_ip(**kwargs)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LocalExecutionHostExposureTests(unittest.TestCase):
    def setUp(self):
        self.config_values = {
            "network.ISOLATE_INTERNAL_CHILDREN": True,
            "network.CONSIDER_DEV_AS_INTERNAL": True,
            "network.DISABLE_EXPOSE_OUTSIDE": False,
            "network.FREE_PORTS_RANGE": [],
            "network.PUBLIC_IP": "",
            "network.EXTERNAL_INTERFACE": "",
        }
        self.execute_kwargs = {}
        self.logs = []

    def _run(self, father_id="dev-1", father_is_local_instance=False):
        resources = celaut.Service.Container.Resources(
            at_init=celaut.Sysresources(mem_limit=1_000_000_000, disk_space=5_000_000_000)
        )
        service = celaut.Service(
            api=celaut.Service.Api(
                slot=[
                    celaut.Service.Api.Slot(port=5000, transport=celaut.Service.Api.Protocol(tags=["tcp"])),
                ]
            )
        )
        resolved = celaut.Sysresources(mem_limit=1_000_000_000, disk_space=5_000_000_000)

        def _execute(**kwargs):
            self.execute_kwargs = kwargs
            return (VM_ID, VM_IP, resolved)

        interfaces = {
            "eth0": {AF_INET: [{"addr": "172.28.10.5"}]},
            "lo": {AF_INET: [{"addr": "127.0.0.1"}]},
        }
        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(local_execute.env_manager, "get",
                               side_effect=lambda key, default=None: self.config_values.get(key, default)))
            enter(patch.object(local_execute, "get_configured_virtualizer", return_value="ch"))
            enter(patch.object(local_execute, "select_virtualizer", return_value="ch"))
            enter(patch.object(local_execute, "build", return_value="svc-hash"))
            enter(patch.object(local_execute, "reserve_instance_name", return_value="tidy-island"))
            enter(patch.object(local_execute.sc, "internal_instance_exists",
                               return_value=father_is_local_instance))
            enter(patch.object(local_execute, "resolve_slot_transport_protocols", return_value="tcp"))
            enter(patch.object(local_execute, "get_free_port", return_value=51000))
            enter(patch.object(local_execute.sc, "add_local_instance"))
            enter(patch.object(local_execute.sc, "set_local_instance_definition"))
            enter(patch.object(local_execute, "execute", side_effect=_execute))
            enter(patch.object(local_execute.log, "LOGGER", side_effect=self.logs.append))
            enter(patch.object(host_interface.ni, "AF_INET", AF_INET))
            enter(patch.object(host_interface.ni, "AF_INET6", AF_INET6))
            enter(patch.object(host_interface.ni, "ifaddresses", side_effect=_fake_ifaddresses(interfaces)))
            enter(patch.object(host_interface.ni, "gateways", return_value={}))
            return local_execute.local_execution(
                config=celaut.Configuration(initial_mu=to_amount(1234)),
                resources=resources,
                father_id=father_id,
                father_ip="127.0.0.1",
                metadata=celaut.Metadata(),
                service=service,
                service_id="svc-hash",
                refund_container=[],
            )

    def _uris(self, response):
        return [(uri.ip, uri.port) for slot in response.instance.uri_slot for uri in slot.uri]

    def test_off_by_default_the_instance_stays_internal(self):
        self.config_values["network.EXTERNAL_INTERFACE"] = "eth0"
        response = self._run()
        self.assertTrue(self.execute_kwargs["by_local"])
        self.assertEqual(self.execute_kwargs["assigment_ports"], {5000: 5000})
        self.assertEqual(self._uris(response), [(VM_IP, 5000)])

    def test_on_with_external_interface_publishes_on_it(self):
        self.config_values[HOST_EXPOSURE_KEY] = True
        self.config_values["network.EXTERNAL_INTERFACE"] = "eth0"
        response = self._run()
        self.assertFalse(self.execute_kwargs["by_local"])  # DNAT on the host
        self.assertEqual(self.execute_kwargs["assigment_ports"], {5000: 51000})
        self.assertEqual(self._uris(response), [("172.28.10.5", 51000)])

    def test_on_with_public_ip_uses_it(self):
        self.config_values[HOST_EXPOSURE_KEY] = True
        self.config_values["network.PUBLIC_IP"] = "203.0.113.9"
        self.config_values["network.EXTERNAL_INTERFACE"] = "eth0"
        response = self._run()
        self.assertEqual(self._uris(response), [("203.0.113.9", 51000)])

    def test_on_with_only_loopback_never_advertises_it_and_stays_internal(self):
        self.config_values[HOST_EXPOSURE_KEY] = True
        for key, value in (("network.EXTERNAL_INTERFACE", "lo"), ("network.PUBLIC_IP", "127.0.0.1")):
            with self.subTest(**{key.split(".")[1]: value}):
                self.config_values["network.EXTERNAL_INTERFACE"] = ""
                self.config_values["network.PUBLIC_IP"] = ""
                self.config_values[key] = value
                self.logs.clear()
                response = self._run()
                self.assertTrue(self.execute_kwargs["by_local"])
                self.assertEqual(self._uris(response), [(VM_IP, 5000)])
                self.assertNotIn("127.0.0.1", [ip for ip, _ in self._uris(response)])
                self.assertTrue(
                    any("ERROR" in line and HOST_EXPOSURE_KEY in line for line in self.logs),
                    self.logs,
                )

    def test_on_with_nothing_configured_and_no_default_route_stays_internal(self):
        self.config_values[HOST_EXPOSURE_KEY] = True
        response = self._run()
        self.assertTrue(self.execute_kwargs["by_local"])
        self.assertEqual(self._uris(response), [(VM_IP, 5000)])

    def test_disable_expose_outside_wins(self):
        self.config_values[HOST_EXPOSURE_KEY] = True
        self.config_values["network.EXTERNAL_INTERFACE"] = "eth0"
        self.config_values["network.DISABLE_EXPOSE_OUTSIDE"] = True
        response = self._run()
        self.assertTrue(self.execute_kwargs["by_local"])
        self.assertEqual(self._uris(response), [(VM_IP, 5000)])

    def test_a_child_of_a_local_instance_stays_isolated(self):
        # Only launches by this node's own local clients are affected; an instance's
        # own children keep ISOLATE_INTERNAL_CHILDREN's answer.
        self.config_values[HOST_EXPOSURE_KEY] = True
        self.config_values["network.EXTERNAL_INTERFACE"] = "eth0"
        response = self._run(father_id="vm-parent", father_is_local_instance=True)
        self.assertTrue(self.execute_kwargs["by_local"])
        self.assertEqual(self._uris(response), [(VM_IP, 5000)])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ExecuteHostExposureNoteTests(unittest.TestCase):
    def _response(self, ip, port):
        return celaut.ServiceInstance(
            token=VM_ID,
            instance=celaut.Instance(
                uri_slot=[celaut.Instance.Uri_Slot(internal_port=5000, uri=[celaut.Instance.Uri(ip=ip, port=port)])]
            ),
        )

    def _note(self, response, values, resolve):
        out = io.StringIO()
        with patch.object(execute_cmd.env_manager, "get",
                          side_effect=lambda key, default=None: values.get(key, default)), \
                patch.object(execute_cmd, "resolve_from_config", side_effect=resolve), \
                contextlib.redirect_stdout(out):
            execute_cmd.print_host_exposure_note(response)
        return out.getvalue()

    def test_off_prints_nothing(self):
        self.assertEqual(self._note(self._response(VM_IP, 5000), {}, lambda get: "172.28.10.5"), "")

    def test_published_endpoint_is_named(self):
        out = self._note(self._response("172.28.10.5", 51000), {HOST_EXPOSURE_KEY: True}, lambda get: "172.28.10.5")
        self.assertIn("Published on this host's interface (172.28.10.5)", out)

    def test_unresolvable_says_why_and_points_at_tunnel(self):
        def _fail(get):
            raise HostInterfaceUnresolved("interface 'lo' has no address other than loopback/link-local")
        out = self._note(self._response(VM_IP, 5000), {HOST_EXPOSURE_KEY: True}, _fail)
        self.assertIn("was not published", out)
        self.assertIn("loopback", out)
        self.assertIn(f"nodo tunnel {VM_ID} 5000", out)
        self.assertNotIn("127.0.0.1", out)


class InstallerEnablesHostExposureTests(unittest.TestCase):
    def test_windows_installer_turns_it_on_next_to_external_interface(self):
        content = Path("bash/install.ps1").read_text(encoding="utf-8")
        interface = content.index('.network.EXTERNAL_INTERFACE = \\"$WSL_IFACE\\"')
        enabled = content.index(".network.EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE = true")
        self.assertLess(interface, enabled)
        self.assertLess(enabled - interface, 300)
        self.assertNotIn("DEFAULT_EXECUTE_REMOTE", content)

    def test_it_ships_disabled(self):
        content = Path("config.example.yaml").read_text(encoding="utf-8")
        self.assertIn("  EXPOSE_LOCAL_EXECUTIONS_ON_HOST_INTERFACE: false\n", content)


if __name__ == "__main__":
    unittest.main()

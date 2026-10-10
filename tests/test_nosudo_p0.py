"""Phase 0 of running nodo without sudo (PR #466): checks that do not lie.

* A sysctl write the kernel refused is a failure, although ``sysctl -w`` exits 0.
* The network guards accept ``CAP_NET_ADMIN``, not only uid 0.
* The control socket directory belongs to the daemon's user, and to nobody else.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import privileges  # noqa: E402
from src.virtualizers.microvm import host  # noqa: E402
from src.virtualizers.microvm.errors import MicroVMError  # noqa: E402


def _proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class WriteSysctlTests(unittest.TestCase):
    def test_a_write_that_took_passes(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return _proc("1\n") if command[1] == "-n" else _proc()

        with mock.patch.object(host, "run", side_effect=fake_run):
            host.write_sysctl("net.ipv4.ip_forward", "1")

        self.assertEqual(
            calls,
            [["sysctl", "-w", "net.ipv4.ip_forward=1"], ["sysctl", "-n", "net.ipv4.ip_forward"]],
        )

    def test_a_refused_write_that_exited_zero_is_a_failure(self):
        """procps prints 'permission denied ... ignoring' and exits 0; the value stays 0."""

        def fake_run(command, **kwargs):
            return _proc("0\n") if command[1] == "-n" else _proc()

        with mock.patch.object(host, "run", side_effect=fake_run):
            with self.assertRaises(MicroVMError) as caught:
                host.write_sysctl("net.ipv4.conf.nodo-br-ch.proxy_arp", "1")

        self.assertIn("CAP_NET_ADMIN", str(caught.exception))

    def test_guest_isolation_reads_every_key_back(self):
        from src.virtualizers.microvm import network

        written = []
        with mock.patch.object(network, "write_sysctl", side_effect=lambda k, v: written.append((k, v))):
            network.ensure_guest_l2_isolation()

        bridge = network.NETWORK_BRIDGE_NAME
        self.assertEqual(
            written,
            [
                (f"net.ipv4.conf.{bridge}.proxy_arp", "1"),
                (f"net.ipv4.conf.{bridge}.proxy_arp_pvlan", "1"),
                (f"net.ipv4.conf.{bridge}.send_redirects", "0"),
            ],
        )


class CapabilityTests(unittest.TestCase):
    def _status(self, cap_eff: str) -> str:
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        handle.write(f"Name:\tpython\nCapInh:\t0000000000000000\nCapEff:\t{cap_eff}\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_net_admin_alone_is_enough_for_a_service_user(self):
        status = self._status("0000000000001000")
        with mock.patch.object(privileges.os, "geteuid", return_value=999):
            self.assertTrue(privileges.has_capability(privileges.CAP_NET_ADMIN, status))
            self.assertFalse(privileges.has_capability(privileges.CAP_SYS_ADMIN, status))

    def test_no_capabilities_is_refused(self):
        status = self._status("0000000000000000")
        with mock.patch.object(privileges.os, "geteuid", return_value=999):
            self.assertFalse(privileges.has_capability(privileges.CAP_NET_ADMIN, status))

    def test_root_passes_as_before(self):
        status = self._status("0000000000000000")
        with mock.patch.object(privileges.os, "geteuid", return_value=0):
            self.assertTrue(privileges.has_capability(privileges.CAP_NET_ADMIN, status))

    def test_an_unreadable_status_file_is_no_capability(self):
        with mock.patch.object(privileges.os, "geteuid", return_value=999):
            self.assertFalse(privileges.has_capability(privileges.CAP_NET_ADMIN, "/nonexistent"))

    def test_the_gateway_port_guard_lets_net_admin_through(self):
        from src.utils.firewall import gateway

        backend = mock.Mock()
        backend.name = "fake"
        backend.ensure_input_accept.return_value = False
        backend.prune_input_accepts.return_value = []
        with mock.patch.object(gateway, "can_admin_network", return_value=True):
            result = gateway.ensure_gateway_port_open(52000, backend=backend, verify=False)

        backend.ensure_input_accept.assert_called_once()
        self.assertIsNotNone(result)

    def test_the_gateway_port_guard_names_the_capability(self):
        from src.utils.firewall import gateway

        with mock.patch.object(gateway, "can_admin_network", return_value=False):
            with self.assertRaises(gateway.GatewayPortUnavailable) as caught:
                gateway.ensure_gateway_port_open(52000, backend=mock.Mock(), verify=False)

        self.assertIn("CAP_NET_ADMIN", caught.exception.summary)


class PrivateDirTests(unittest.TestCase):
    def test_creates_the_directory_for_this_user_only(self):
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "run", "ch")
            host.ensure_private_dir(path)
            info = os.stat(path)
            self.assertEqual(info.st_uid, os.geteuid())
            self.assertEqual(stat.S_IMODE(info.st_mode) & 0o022, 0)

    def test_tightens_a_world_writable_directory_it_owns(self):
        with tempfile.TemporaryDirectory() as base:
            os.chmod(base, 0o777)
            host.ensure_private_dir(base)
            self.assertEqual(stat.S_IMODE(os.stat(base).st_mode) & 0o022, 0)

    def test_refuses_a_directory_another_user_owns(self):
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.object(host.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaises(MicroVMError) as caught:
                    host.ensure_private_dir(base)
            self.assertIn("API_SOCKET_DIR", str(caught.exception))

    def test_the_default_is_not_under_tmp(self):
        from src.virtualizers.microvm import paths

        with mock.patch.object(paths.env_manager, "get", side_effect=lambda key, default=None: default):
            self.assertEqual(str(paths.control_socket_dir()), "/run/nodo/ch")


if __name__ == "__main__":
    unittest.main()

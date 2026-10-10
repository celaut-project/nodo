"""Phase 2 of running nodo without sudo (PR #466): the daemon as a service user.

The daemon keeps CAP_NET_ADMIN only; the hypervisor and virtiofsd get none. What
that needs from the code, pinned here:

* taps owned by the daemon's user, so a hypervisor without capabilities can open them;
* children exec'd with the ambient set cleared;
* virtiofsd confined with a namespace sandbox when chroot is not available;
* per-VM cgroups under the unit's delegated cgroup;
* an nft table of its own for a second install on one host;
* a unit template that doctor renders from the same config.
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = Path(__file__).resolve().parent.parent


class TapOwnerTests(unittest.TestCase):
    def test_the_tap_is_created_for_this_user_and_brought_up(self):
        from src.virtualizers.microvm import network

        calls = []

        def fake_run(command, check=True, **kwargs):
            calls.append(command)
            result = mock.Mock()
            result.returncode = 1 if command[:3] == ["ip", "link", "show"] else 0
            return result

        with mock.patch.object(network, "run", side_effect=fake_run), mock.patch.object(
            network.os, "geteuid", return_value=995
        ):
            tap = network.create_tap("vm-1")

        self.assertIn(["ip", "tuntap", "add", "dev", tap, "mode", "tap", "user", "995"], calls)
        self.assertEqual(calls[-1], ["ip", "link", "set", tap, "up"])


class ExecWithoutCapabilitiesTests(unittest.TestCase):
    def test_a_root_daemon_leaves_its_children_alone(self):
        from src.utils import privileges

        prctl = mock.Mock(return_value=0)
        with mock.patch.object(privileges, "_PRCTL", prctl), mock.patch.object(
            privileges.os, "geteuid", return_value=0
        ):
            privileges.exec_without_capabilities()
        prctl.assert_not_called()

    def test_a_service_user_clears_the_ambient_set(self):
        from src.utils import privileges

        prctl = mock.Mock(return_value=0)
        with mock.patch.object(privileges, "_PRCTL", prctl), mock.patch.object(
            privileges.os, "geteuid", return_value=995
        ):
            privileges.exec_without_capabilities()
        prctl.assert_called_once_with(47, 4, 0, 0, 0)

    def test_a_failed_clear_stops_the_exec(self):
        from src.utils import privileges

        with mock.patch.object(privileges, "_PRCTL", mock.Mock(return_value=-1)), mock.patch.object(
            privileges.os, "geteuid", return_value=995
        ):
            with self.assertRaises(OSError):
                privileges.exec_without_capabilities()


class VirtiofsdSandboxTests(unittest.TestCase):
    def test_auto_is_chroot_for_root_and_namespace_otherwise(self):
        from src.virtualizers.microvm import shares

        with mock.patch.object(shares.os, "geteuid", return_value=0):
            self.assertEqual(shares.virtiofsd_sandbox("auto"), "chroot")
        with mock.patch.object(shares.os, "geteuid", return_value=995):
            self.assertEqual(shares.virtiofsd_sandbox("auto"), "namespace")
            self.assertEqual(shares.virtiofsd_sandbox(""), "namespace")

    def test_an_explicit_mode_is_kept_and_a_typo_refused(self):
        from src.virtualizers.microvm import shares
        from src.virtualizers.microvm.errors import MicroVMError

        self.assertEqual(shares.virtiofsd_sandbox("none"), "none")
        with self.assertRaises(MicroVMError):
            shares.virtiofsd_sandbox("chroots")


class DelegatedCgroupTests(unittest.TestCase):
    def setUp(self):
        from src.virtualizers.microvm import cgroups

        self.cgroups = cgroups
        self.mount = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.mount, ignore_errors=True))
        self.unit = self.mount / "system.slice" / "nodo.service"
        self.unit.mkdir(parents=True)
        (self.unit / "cgroup.procs").write_text("101\n102\n")
        self.proc_cgroup = self.mount / "self-cgroup"

    def _run(self, own: str, base: Path):
        self.proc_cgroup.write_text(f"0::{own}\n")
        real_open = open
        moved = []

        def fake_open(path, mode="r", *args, **kwargs):
            path = str(path)
            if path == "/proc/self/cgroup":
                return real_open(self.proc_cgroup, mode, *args, **kwargs)
            if path.endswith("supervisor/cgroup.procs") and "w" in mode:
                handle = mock.MagicMock()
                handle.__enter__.return_value.write.side_effect = moved.append
                return handle
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(self.cgroups, "CGROUP_MOUNT", self.mount), mock.patch.object(
            self.cgroups, "CGROUPS_BASE_DIR", str(base)
        ), mock.patch("builtins.open", side_effect=fake_open):
            self.cgroups.leave_delegated_base()
        return moved

    def test_the_daemon_moves_into_a_leaf_of_its_own_cgroup(self):
        moved = self._run("/system.slice/nodo.service", self.unit)
        self.assertTrue((self.unit / "supervisor").is_dir())
        self.assertEqual(moved, ["101\n", "102\n"])

    def test_nothing_moves_when_the_base_is_the_root(self):
        moved = self._run("/system.slice/nodo.service", self.mount)
        self.assertEqual(moved, [])
        self.assertFalse((self.mount / "supervisor").exists())

    def test_nothing_moves_when_the_base_is_another_cgroup(self):
        moved = self._run("/user.slice/session-1.scope", self.unit)
        self.assertEqual(moved, [])


class NftTableTests(unittest.TestCase):
    def tearDown(self):
        from src.utils.firewall import backends

        backends.set_nft_table("nodo")

    def test_a_second_install_names_its_own_table(self):
        from src.utils.firewall import backends
        from src.utils.firewall.rules import Chain

        backends.set_nft_table("nodotest")
        commands = []

        def runner(command):
            commands.append(list(command))
            return mock.Mock(returncode=0, stdout='{"nftables": []}', stderr="")

        backends.NftBackend(run=runner).list_rules(Chain.INPUT)
        self.assertEqual(commands[-1][:6], ["nft", "-j", "list", "chain", "inet", "nodotest"])

    def test_a_bad_name_is_refused(self):
        from src.utils.firewall import backends
        from src.utils.firewall.errors import FirewallError

        for name in ("", "1nodo", "no do", "a;b", "x" * 40):
            with self.assertRaises(FirewallError, msg=name):
                backends.set_nft_table(name)

    def test_the_config_key_reaches_the_backend(self):
        from src.utils import config as config_module
        from src.utils.firewall import backends

        manager = config_module.ConfigManager.__new__(config_module.ConfigManager)
        manager._config = {"virtualizers": {"ch": {"NFT_TABLE": "nodotest"}}}
        manager._apply_nft_table()
        self.assertEqual(backends.NFT_TABLE, "nodotest")

    def test_the_default_does_not_import_the_firewall_package(self):
        from src.utils import config as config_module

        manager = config_module.ConfigManager.__new__(config_module.ConfigManager)
        manager._config = {"virtualizers": {"ch": {"NFT_TABLE": "nodo"}}}
        with mock.patch("src.utils.firewall.backends.set_nft_table") as setter:
            manager._apply_nft_table()
        setter.assert_not_called()


class UnitTemplateTests(unittest.TestCase):
    def _render(self, service_user: str) -> str:
        from src.commands import doctor

        main_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(main_dir, ignore_errors=True))
        with open(os.path.join(main_dir, "config.yaml"), "w") as handle:
            handle.write(f'main:\n  SERVICE_USER: "{service_user}"\n')
        name = doctor._service_template_name(main_dir)
        template = (_ROOT / "bash" / name).read_text()
        with mock.patch.object(doctor, "_resolve_admin_group", return_value="sudo"):
            return name, doctor._render_service_template(template, main_dir)

    def test_a_root_install_keeps_the_root_unit(self):
        name, rendered = self._render("")
        self.assertEqual(name, "nodo.service.template")
        self.assertIn("User=root", rendered)

    def test_a_service_user_install_gets_the_capability_unit(self):
        name, rendered = self._render("nodo")
        self.assertEqual(name, "nodo-nosudo.service.template")
        for line in (
            "User=nodo",
            "Group=nodo",
            "SupplementaryGroups=kvm",
            "AmbientCapabilities=CAP_NET_ADMIN",
            "Delegate=yes",
            "RuntimeDirectory=nodo",
        ):
            self.assertIn(line, rendered.splitlines())
        self.assertNotIn("User=root", rendered)
        self.assertNotIn("CapabilityBoundingSet", "\n".join(
            l for l in rendered.splitlines() if not l.startswith("#")
        ))

    def test_the_setup_script_and_doctor_know_the_same_placeholders(self):
        template = (_ROOT / "bash" / "nodo-nosudo.service.template").read_text()
        script = (_ROOT / "bash" / "setup_service_user.sh").read_text()
        doctor_src = (_ROOT / "src" / "commands" / "doctor.py").read_text()
        for placeholder in sorted(set(re.findall(r"{{[A-Z_]+}}", template))):
            self.assertIn(placeholder, script, placeholder)
            self.assertIn(f'"{placeholder}"', doctor_src, placeholder)

    def test_install_sh_offers_the_flag_and_keeps_the_choice(self):
        install = (_ROOT / "install.sh").read_text()
        self.assertIn("--service-user)", install)
        self.assertIn("bash/setup_service_user.sh", install)
        self.assertIn(".main.SERVICE_USER", install)


if __name__ == "__main__":
    unittest.main()

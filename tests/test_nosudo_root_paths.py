"""What a step that runs as root may take from config.yaml on a no-sudo node.

On a node whose daemon is a service user, config.yaml is that user's to write.
``sudo nodo doctor``, ``sudo nodo update`` (install.sh) and
``bash/setup_service_user.sh`` run as root, so none of them may let it choose
what root does: who the unit runs as comes from a root-owned record, and every
value that goes into a unit or a root step is checked first.
"""

import getpass
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
LIB_PRIVILEGED = _ROOT / "bash" / "lib_privileged.sh"
LIB_PKG = _ROOT / "bash" / "lib_pkg.sh"


def _bash(script: str, **env) -> subprocess.CompletedProcess:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(
        ["bash", "-c", f'. "{LIB_PKG}"; . "{LIB_PRIVILEGED}"; {script}'],
        capture_output=True, text=True, env=environment, stdin=subprocess.DEVNULL,
    )


def _root_owned(real_stat):
    """``os.stat`` that reports every file as root's and writable only by root."""

    def fake(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        return types.SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022)

    return fake


class ServiceUserRecordTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.record = os.path.join(self.dir, "service-user")
        self.unit = os.path.join(self.dir, "nodo.service")

    def _installed(self):
        from src.utils import privileges

        with mock.patch.object(privileges.os, "stat", _root_owned(os.stat)):
            return privileges.installed_service_user(record=self.record, unit_file=self.unit)

    def test_the_record_names_the_user(self):
        Path(self.record).write_text("nodo")
        self.assertEqual(self._installed(), "nodo")

    def test_without_a_record_the_installed_unit_decides(self):
        Path(self.unit).write_text("[Service]\nUser=nodo\n")
        self.assertEqual(self._installed(), "nodo")
        Path(self.unit).write_text("[Service]\nUser=nodo\nUser=root\n")
        self.assertEqual(self._installed(), "")
        os.unlink(self.unit)
        self.assertEqual(self._installed(), "")

    def test_a_record_others_can_write_is_refused(self):
        from src.utils import privileges

        Path(self.record).write_text("nodo")
        os.chmod(self.record, 0o666)
        with self.assertRaises(ValueError):
            privileges.installed_service_user(record=self.record, unit_file=self.unit)

    def test_a_record_with_more_than_one_name_is_refused(self):
        for content in ("nodo\nroot", "", "no do", "nodo;id", "../x"):
            with self.subTest(content=content):
                Path(self.record).write_text(content)
                with self.assertRaises(ValueError):
                    self._installed()

    def test_bash_reads_the_same_sources(self):
        Path(self.unit).write_text("[Service]\nUser=nodo\n")
        missing = os.path.join(self.dir, "none")
        result = _bash(f'installed_service_user "{self.unit}"', NODO_SERVICE_USER_RECORD=missing)
        self.assertEqual((result.returncode, result.stdout), (0, "nodo"))

        Path(self.record).write_text("nodo")
        os.chmod(self.record, 0o666)
        result = _bash(
            f'NODO_SERVICE_USER_RECORD="{self.record}"; installed_service_user "{self.unit}"'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writable only by root", result.stderr)

    def test_an_empty_service_user_in_config_does_not_make_a_root_unit(self):
        """Review of #533, item 1: SERVICE_USER: "" in config.yaml chose the root unit."""
        from src.commands import doctor

        main_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(main_dir, ignore_errors=True))
        Path(main_dir, "config.yaml").write_text('main:\n  SERVICE_USER: ""\n')
        name = doctor._service_template_name("nodo")
        template = (_ROOT / "bash" / name).read_text()
        rendered = doctor._render_service_template(template, main_dir, "nodo")
        self.assertIn("User=nodo", rendered.splitlines())
        self.assertNotIn("User=root", rendered)

    def test_no_root_path_reads_the_user_from_config(self):
        for path in ("install.sh", "nodo.py", "src/commands/doctor.py", "bash/setup_service_user.sh"):
            with self.subTest(path=path):
                self.assertNotIn("SERVICE_USER'", (_ROOT / path).read_text())
                self.assertNotIn('main.SERVICE_USER"', (_ROOT / path).read_text())
                self.assertNotIn(".main.SERVICE_USER", (_ROOT / path).read_text())


# Values the review of #533 named, and their cousins: each one would add a line or
# run a command in a unit that root writes.
INJECTIONS = (
    "/x\nUser=root",
    "/x\\\nUser=root",
    "/x\\",
    "/x\x00y",
    "/x'; id; '",
    '/x"y',
    "/x$(id)",
    "/x`id`",
    "/x%n",
    "/x:/evil",
    "/x;id",
    "relative/path",
)


class UnitValueTests(unittest.TestCase):
    def setUp(self):
        self.main_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.main_dir, ignore_errors=True))

    def _render(self, java_home, service_user="nodo"):
        import yaml
        from src.commands import doctor

        Path(self.main_dir, "config.yaml").write_text(
            yaml.safe_dump({"dependencies": {"java": {"JAVA_HOME": java_home}}})
        )
        name = doctor._service_template_name(service_user)
        template = (_ROOT / "bash" / name).read_text()
        with mock.patch.object(doctor, "_resolve_admin_group", return_value="sudo"):
            return doctor._render_service_template(template, self.main_dir, service_user)

    def test_doctor_refuses_every_injection(self):
        for user in ("nodo", ""):
            for value in INJECTIONS:
                with self.subTest(user=user, value=value):
                    with self.assertRaises(ValueError):
                        self._render(value, user)

    def test_doctor_refuses_a_value_that_is_not_text(self):
        with self.assertRaises(ValueError):
            self._render(["/x"])

    def test_a_path_with_a_space_is_quoted_in_the_no_sudo_unit(self):
        rendered = self._render("/home/a b/java")
        self.assertIn('Environment="JAVA_HOME=/home/a b/java"', rendered.splitlines())
        self.assertEqual(rendered.count("User="), 1)

    def test_bash_refuses_every_injection(self):
        for value in INJECTIONS + ("",):
            if "\x00" in value:
                continue  # bash strings cannot hold a NUL
            with self.subTest(value=value):
                result = _bash('require_unit_paths "JAVA_HOME=$V"', V=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot go into the systemd unit", result.stderr)
        result = _bash('require_unit_paths "a=/nodo" "b=/home/a b/.local/share/nodo"; echo ok')
        self.assertEqual(result.stdout.strip(), "ok")

    def test_every_renderer_checks_before_it_writes(self):
        install = (_ROOT / "install.sh").read_text()
        for function in ("create_service_file() {", "prepare_unprivileged_service() {"):
            body = install.split(function)[1].split("\n}\n")[0]
            with self.subTest(function=function):
                self.assertLess(body.index("require_unit_paths"), body.index("sed \\"))
        script = (_ROOT / "bash" / "setup_service_user.sh").read_text()
        self.assertLess(script.index("require_unit_paths"), script.index('"$TEMPLATE" > "$rendered"'))

    def test_sed_escapes_a_backslash(self):
        for path in ("install.sh", "bash/setup_service_user.sh"):
            with self.subTest(path=path):
                self.assertIn("""sed 's/[&|\\\\]/\\\\&/g'""", (_ROOT / path).read_text())


@unittest.skipUnless(sys.platform.startswith("linux"), "GNU realpath and stat")
class StorageTests(unittest.TestCase):
    """Review of #533, item 1: setup_service_user.sh chowned whatever STORAGE named."""

    def setUp(self):
        self.target = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.target, ignore_errors=True))
        self.outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.outside, ignore_errors=True))

    def _storage(self, storage, user="nobody"):
        return _bash(f'service_storage_dir "$T" "$S" "{user}"', T=self.target, S=storage)

    def test_the_storage_inside_the_install_is_accepted(self):
        result = self._storage(f"{self.target}/storage")
        self.assertEqual((result.returncode, result.stdout), (0, f"{self.target}/storage"))
        self.assertTrue(os.path.isdir(f"{self.target}/storage"))

    def test_a_path_outside_the_install_is_refused(self):
        for storage in ("/etc", "/", f"{self.target}/storage/../../etc", self.outside, f"{self.target}"):
            with self.subTest(storage=storage):
                result = self._storage(storage)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be", result.stderr)

    def test_another_directory_of_the_install_is_refused(self):
        for name in ("bin", "venv", "bash", "storage/sub"):
            with self.subTest(name=name):
                self.assertNotEqual(self._storage(f"{self.target}/{name}").returncode, 0)

    def test_a_symlink_cannot_lead_the_chown_out(self):
        os.symlink("/etc", f"{self.target}/link")
        for storage in (f"{self.target}/link", f"{self.target}/link/../storage/x"):
            with self.subTest(storage=storage):
                self.assertNotEqual(self._storage(storage).returncode, 0)
        # ".." is taken as written, not through the symlink: this is <install>/storage.
        result = self._storage(f"{self.target}/link/../storage")
        self.assertEqual(result.stdout, f"{self.target}/storage")

    def test_the_operators_symlink_to_another_disk_is_followed(self):
        os.symlink(self.outside, f"{self.target}/storage")
        result = self._storage(f"{self.target}/storage")
        self.assertEqual((result.returncode, result.stdout), (0, self.outside))

    def test_an_install_dir_the_user_can_change_is_refused(self):
        result = self._storage(f"{self.target}/storage", user=getpass.getuser())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("can be changed by", result.stderr)

    def test_the_operators_own_install_is_accepted_when_the_daemon_is_the_operator(self):
        me = getpass.getuser()
        result = _bash(f'service_storage_dir "$T" "$T/storage" "{me}" "{me}"', T=self.target)
        self.assertEqual((result.returncode, result.stdout), (0, f"{self.target}/storage"))


@unittest.skipUnless(sys.platform.startswith("linux"), "GNU realpath and stat")
class PathWritableByTests(unittest.TestCase):
    def _writable(self, path, user):
        return _bash(f'path_writable_by "$P" "{user}"', P=path).returncode == 0

    def test_owner_group_other_and_missing_parts(self):
        me = getpass.getuser()
        mine = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(mine, ignore_errors=True))
        self.assertTrue(self._writable(f"{mine}/missing/bin", me))
        self.assertTrue(self._writable("/tmp/surely-missing-nodo/bin", "nobody"))
        self.assertFalse(self._writable("/usr/bin/env", "nobody"))
        self.assertFalse(self._writable("/usr/bin/env", "no-such-user-nodo"))
        os.symlink(mine, f"{mine}.link")
        self.addCleanup(os.unlink, f"{mine}.link")
        self.assertTrue(self._writable(f"{mine}.link/x", me))


class DoctorUnitUserTests(unittest.TestCase):
    """Review of #533, item 1: doctor rewrote and enabled a User=root unit, no prompt."""

    def setUp(self):
        from src.commands import doctor

        self.doctor = doctor
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.main_dir = os.path.join(self.dir, "nodo")
        os.makedirs(os.path.join(self.main_dir, "bash"))
        for name in ("nodo.service.template", "nodo-nosudo.service.template"):
            Path(self.main_dir, "bash", name).write_text((_ROOT / "bash" / name).read_text())
        # What a compromised daemon writes: no user, and a newline-free but
        # otherwise ordinary config.
        Path(self.main_dir, "config.yaml").write_text('main:\n  SERVICE_USER: ""\n')
        self.unit = os.path.join(self.dir, "nodo.service")
        self.calls = []

    def _render(self, user):
        name = self.doctor._service_template_name(user)
        template = Path(self.main_dir, "bash", name).read_text()
        with mock.patch.object(self.doctor, "_resolve_admin_group", return_value="sudo"):
            return self.doctor._render_service_template(template, self.main_dir, user)

    def _run(self, record_user, tty=False, answer="n"):
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        patches = [
            mock.patch.object(self.doctor, "SERVICE_FILE_PATH", self.unit),
            mock.patch.object(self.doctor.os, "geteuid", return_value=0),
            mock.patch.object(self.doctor, "installed_service_user", return_value=record_user),
            mock.patch.object(self.doctor, "_resolve_admin_group", return_value="sudo"),
            mock.patch.object(self.doctor, "_doctor_kvm_checks"),
            mock.patch.object(self.doctor, "_doctor_cloud_hypervisor"),
            mock.patch.object(self.doctor, "_doctor_network_checks"),
            mock.patch.object(self.doctor.subprocess, "run", side_effect=lambda c, **k: self.calls.append(c)),
            mock.patch.object(self.doctor.sys, "stdin", stdin),
            mock.patch("builtins.input", return_value=answer),
            mock.patch("builtins.print"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.doctor.doctor_command(self.main_dir)

    def test_the_service_users_unit_is_kept_whatever_config_says(self):
        Path(self.unit).write_text(self._render("nodo"))
        before = Path(self.unit).read_text()
        self._run("nodo")
        self.assertEqual(Path(self.unit).read_text(), before)
        self.assertEqual(self.calls, [])

    def test_a_user_change_is_not_made_without_a_terminal(self):
        Path(self.unit).write_text(self._render("nodo"))
        before = Path(self.unit).read_text()
        self._run("")
        self.assertEqual(Path(self.unit).read_text(), before)
        self.assertNotIn(["systemctl", "enable", "nodo.service"], self.calls)
        self.assertNotIn(["systemctl", "stop", "nodo.service"], self.calls)

    def test_a_user_change_is_not_made_when_the_operator_says_no(self):
        Path(self.unit).write_text(self._render("nodo"))
        before = Path(self.unit).read_text()
        self._run("", tty=True, answer="")
        self.assertEqual(Path(self.unit).read_text(), before)

    def test_a_confirmed_user_change_is_made(self):
        Path(self.unit).write_text(self._render("nodo"))
        self._run("", tty=True, answer="y")
        self.assertIn("User=root", Path(self.unit).read_text().splitlines())
        self.assertIn(["systemctl", "enable", "nodo.service"], self.calls)

    def test_a_drifted_unit_of_the_same_user_is_still_fixed(self):
        Path(self.unit).write_text(self._render("nodo").replace("Delegate=yes", "Delegate=no"))
        self._run("nodo")
        self.assertIn("Delegate=yes", Path(self.unit).read_text().splitlines())

    def test_an_injected_value_is_not_written(self):
        Path(self.main_dir, "config.yaml").write_text(
            'dependencies:\n  java:\n    JAVA_HOME: "/x\\nUser=root"\n'
        )
        Path(self.unit).write_text("[Service]\nUser=nodo\n")
        self._run("nodo")
        self.assertEqual(Path(self.unit).read_text(), "[Service]\nUser=nodo\n")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

"""What a step that runs as root may take from config.yaml on a no-sudo node.

On a node whose daemon is a service user, config.yaml is that user's to write.
``sudo nodo doctor``, ``sudo nodo update`` (install.sh) and
``bash/setup_service_user.sh`` run as root, so none of them may let it choose
what root does: who the unit runs as comes from a root-owned record, and every
value that goes into a unit or a root step is checked first.
"""

import os
import subprocess
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


if __name__ == "__main__":
    unittest.main()

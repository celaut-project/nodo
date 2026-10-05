"""Which virtiofsd the host has, for `nodo doctor` and for the launch check (#478).

The node starts the Rust virtiofsd with --socket-path/--shared-dir/--sandbox/--cache.
The old QEMU C daemon (/usr/lib/qemu/virtiofsd) answers to the same name and does
not take those flags, so the probe must tell the two apart, and a missing daemon
must be a warning in doctor: only services with shared directories need it.
"""
import io
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from src.commands import doctor
from src.virtualizers.microvm import virtiofsd

RUST_OUTPUT = "virtiofsd 1.14.0\n"
# Verbatim from qemu-system-common 1:6.2+dfsg-2ubuntu6.31 on Ubuntu 22.04.
LEGACY_OUTPUT = (
    "virtiofsd version 6.2.0 (Debian 1:6.2+dfsg-2ubuntu6.31)\n"
    "Copyright (c) 2003-2021 Fabrice Bellard and the QEMU Project developers\n"
    "using FUSE kernel interface version 7.36\n"
)


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class ProbeTests(unittest.TestCase):
    def test_rust_virtiofsd_is_usable(self):
        with patch.object(virtiofsd.shutil, "which", return_value="/nodo/bin/virtiofsd"), \
             patch.object(virtiofsd.subprocess, "run", return_value=_completed(RUST_OUTPUT)) as run:
            result = virtiofsd.probe("/nodo/bin/virtiofsd")

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["/nodo/bin/virtiofsd", "--version"])
        self.assertEqual(result.status, virtiofsd.OK)
        self.assertTrue(result.usable)
        self.assertEqual(result.version, "1.14.0")
        self.assertIn("Rust virtiofsd 1.14.0", result.detail)

    def test_old_qemu_c_virtiofsd_is_not_usable(self):
        path = virtiofsd.LEGACY_QEMU_PATH
        with patch.object(virtiofsd.shutil, "which", return_value=path), \
             patch.object(virtiofsd.subprocess, "run", return_value=_completed(LEGACY_OUTPUT)):
            result = virtiofsd.probe(path)

        self.assertEqual(result.status, virtiofsd.LEGACY)
        self.assertFalse(result.usable)
        self.assertEqual(result.version, "6.2.0")
        self.assertIn("old QEMU C virtiofsd", result.detail)
        self.assertIn("--shared-dir", result.detail)

    def test_old_daemon_is_recognized_by_its_output_at_any_path(self):
        with patch.object(virtiofsd.shutil, "which", return_value="/usr/local/bin/virtiofsd"), \
             patch.object(virtiofsd.subprocess, "run", return_value=_completed(LEGACY_OUTPUT)):
            result = virtiofsd.probe("virtiofsd")
        self.assertEqual(result.status, virtiofsd.LEGACY)

    def test_missing_binary_does_not_run_anything(self):
        with patch.object(virtiofsd.shutil, "which", return_value=None), \
             patch.object(virtiofsd.os, "access", return_value=False), \
             patch.object(virtiofsd.subprocess, "run") as run:
            result = virtiofsd.probe("virtiofsd")

        run.assert_not_called()
        self.assertEqual(result.status, virtiofsd.MISSING)
        self.assertIsNone(result.path)
        self.assertIn("not on the node's PATH", result.detail)

    def test_missing_binary_names_the_old_daemon_when_it_is_there(self):
        with patch.object(virtiofsd.shutil, "which", return_value=None), \
             patch.object(virtiofsd.os, "access", return_value=True):
            result = virtiofsd.probe("virtiofsd")
        self.assertEqual(result.status, virtiofsd.MISSING)
        self.assertIn(virtiofsd.LEGACY_QEMU_PATH, result.detail)

    def test_missing_absolute_path(self):
        with patch.object(virtiofsd.shutil, "which", return_value=None):
            result = virtiofsd.probe("/home/jse/.cargo/bin/virtiofsd")
        self.assertEqual(result.status, virtiofsd.MISSING)
        self.assertIn("/home/jse/.cargo/bin/virtiofsd does not exist", result.detail)

    def test_empty_config_value_falls_back_to_the_default_name(self):
        with patch.object(virtiofsd.shutil, "which", return_value=None) as which:
            result = virtiofsd.probe("")
        which.assert_called_once_with(virtiofsd.DEFAULT_BINARY)
        self.assertEqual(result.configured, virtiofsd.DEFAULT_BINARY)

    def test_binary_that_cannot_run_is_unusable(self):
        with patch.object(virtiofsd.shutil, "which", return_value="/nodo/bin/virtiofsd"), \
             patch.object(virtiofsd.subprocess, "run", side_effect=OSError(8, "Exec format error")):
            result = virtiofsd.probe("/nodo/bin/virtiofsd")
        self.assertEqual(result.status, virtiofsd.UNUSABLE)
        self.assertIn("Exec format error", result.detail)

    def test_unknown_output_is_unusable(self):
        with patch.object(virtiofsd.shutil, "which", return_value="/usr/bin/virtiofsd"), \
             patch.object(virtiofsd.subprocess, "run",
                          return_value=_completed(stderr="unknown option", returncode=2)):
            result = virtiofsd.probe("virtiofsd")
        self.assertEqual(result.status, virtiofsd.UNUSABLE)
        self.assertIn("exited 2", result.detail)

    def test_require_usable_raises_with_the_install_steps(self):
        with patch.object(virtiofsd.shutil, "which", return_value=None):
            with self.assertRaises(virtiofsd.VirtiofsdUnavailable) as ctx:
                virtiofsd.require_usable("virtiofsd")
        message = str(ctx.exception)
        self.assertIn("shared directories", message)
        self.assertIn("cargo install virtiofsd --locked", message)
        self.assertIn(virtiofsd.CONFIG_KEY, message)

    def test_require_usable_returns_the_probe_when_usable(self):
        with patch.object(virtiofsd.shutil, "which", return_value="/nodo/bin/virtiofsd"), \
             patch.object(virtiofsd.subprocess, "run", return_value=_completed(RUST_OUTPUT)):
            result = virtiofsd.require_usable("/nodo/bin/virtiofsd")
        self.assertTrue(result.usable)


class DoctorVirtiofsdTests(unittest.TestCase):
    def _doctor(self, which, output=""):
        out = io.StringIO()
        with patch.object(virtiofsd.shutil, "which", return_value=which), \
             patch.object(virtiofsd.os, "access", return_value=False), \
             patch.object(virtiofsd.subprocess, "run", return_value=_completed(output)), \
             redirect_stdout(out):
            result = doctor._doctor_virtiofsd("virtiofsd")
        return result, out.getvalue()

    def test_rust_binary_is_ok(self):
        result, text = self._doctor("/nodo/bin/virtiofsd", RUST_OUTPUT)
        self.assertTrue(result.usable)
        self.assertIn("[OK] /nodo/bin/virtiofsd is the Rust virtiofsd 1.14.0.", text)
        self.assertNotIn("[WARN]", text)

    def test_old_c_binary_is_a_warning_naming_the_implementation(self):
        result, text = self._doctor(virtiofsd.LEGACY_QEMU_PATH, LEGACY_OUTPUT)
        self.assertEqual(result.status, virtiofsd.LEGACY)
        self.assertIn("[WARN]", text)
        self.assertIn("old QEMU C virtiofsd 6.2.0", text)
        self.assertNotIn("[FAIL]", text)

    def test_missing_binary_is_a_warning_not_a_failure(self):
        result, text = self._doctor(None)
        self.assertEqual(result.status, virtiofsd.MISSING)
        self.assertIn("[WARN] No virtiofsd", text)
        self.assertIn("Other services are not affected", text)
        self.assertIn("Suggestion:", text)
        self.assertNotIn("[FAIL]", text)

    def test_config_path_is_read_and_interpolated(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as main_dir:
            with open(os.path.join(main_dir, "config.yaml"), "w") as f:
                f.write(
                    "main:\n  MAIN_DIR: /srv/nodo\n"
                    "virtualizers:\n  ch:\n"
                    "    VIRTIOFSD_BINARY: \"${main.MAIN_DIR}/bin/virtiofsd\"\n"
                )
            cfg = doctor._resolve_config_paths(main_dir)
            self.assertEqual(cfg["virtiofsd_binary"], "/srv/nodo/bin/virtiofsd")

            with open(os.path.join(main_dir, "config.yaml"), "w") as f:
                f.write("virtualizers:\n  ch: {}\n")
            cfg = doctor._resolve_config_paths(main_dir)
            self.assertEqual(cfg["virtiofsd_binary"], "virtiofsd")


if __name__ == "__main__":
    unittest.main()

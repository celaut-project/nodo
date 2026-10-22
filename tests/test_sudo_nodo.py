"""The command a message tells the operator to run as root has to work for them.

An install without sudo puts the `nodo` wrapper in ~/.local/bin, which sudo does
not search, so a printed `sudo nodo serve` ends in "command not found".
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.privileges import sudo_nodo


class SudoNodo(unittest.TestCase):
    def test_wrapper_on_the_sudo_path_is_named_bare(self):
        with mock.patch("shutil.which", return_value="/usr/local/bin/nodo"):
            self.assertEqual(sudo_nodo("serve"), "sudo nodo serve")

    def test_wrapper_off_the_sudo_path_is_named_by_its_path(self):
        with mock.patch("shutil.which", return_value="/home/ana/.local/bin/nodo"):
            self.assertEqual(
                sudo_nodo("daemon restart"), "sudo /home/ana/.local/bin/nodo daemon restart"
            )

    def test_path_with_spaces_is_quoted(self):
        with mock.patch("shutil.which", return_value="/home/ana b/.local/bin/nodo"):
            self.assertEqual(sudo_nodo("serve"), "sudo '/home/ana b/.local/bin/nodo' serve")

    def test_wrapper_not_on_the_users_path_is_found_in_local_bin(self):
        with tempfile.TemporaryDirectory() as home:
            wrapper = os.path.join(home, ".local", "bin", "nodo")
            os.makedirs(os.path.dirname(wrapper))
            with open(wrapper, "w") as handle:
                handle.write("#!/bin/bash\n")
            os.chmod(wrapper, 0o755)
            with mock.patch("shutil.which", return_value=None), mock.patch.dict(
                os.environ, {"HOME": home}
            ):
                self.assertEqual(sudo_nodo("serve"), f"sudo {wrapper} serve")

    def test_no_wrapper_anywhere_falls_back_to_the_bare_name(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch("shutil.which", return_value=None), mock.patch.dict(
                os.environ, {"HOME": home}
            ):
                self.assertEqual(sudo_nodo("serve"), "sudo nodo serve")


if __name__ == "__main__":
    unittest.main()

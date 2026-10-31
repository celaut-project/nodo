"""uninstall.sh keeps every file that holds mnemonics unless --delete-wallets.

The script is never run as a whole: it is copied to a temporary directory, cut
before its final call, and only plan_uninstall and keep_wallets are called, on a
temporary TARGET_DIR with a temporary HOME.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WALLET_NAMES = ["config.yaml", "config-x.yaml", ".config-root.abc", ".config-abc123.yaml"]

DRIVER = r"""
set -u
source "$1/uninstall.sh" --target-dir "$2/nodo" ${3:-}
plan_uninstall
printf 'WALLET:%s\n' "${WALLET_FILES[@]}"
printf 'KEEP:%s\n' "${PLAN_KEEP[@]}"
printf 'REMOVE:%s\n' "${PLAN_REMOVE[@]}"
keep_wallets
printf 'WALLET_DIR:%s\n' "$WALLET_DIR"
"""


class UninstallKeepsWalletFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.scripts = self.tmp / "scripts"
        (self.scripts / "bash").mkdir(parents=True)
        shutil.copy(ROOT / "bash" / "lib_privileged.sh", self.scripts / "bash")
        text = (ROOT / "uninstall.sh").read_text()
        # Drop the final call, so that sourcing it runs nothing.
        text = re.sub(r"(?m)^confirm_uninstall\n.*\Z", "", text, flags=re.S)
        (self.scripts / "uninstall.sh").write_text(text)
        self.target = self.tmp / "nodo"
        self.target.mkdir()
        (self.target / "nodo.py").write_text("")
        for name in WALLET_NAMES:
            (self.target / name).write_text("MNEMONIC: x\n")
        (self.tmp / "home").mkdir()

    def run_driver(self, extra=""):
        env = dict(os.environ, HOME=str(self.tmp / "home"))
        env.pop("XDG_DATA_HOME", None)
        result = subprocess.run(
            ["bash", "-c", DRIVER, "driver", str(self.scripts), str(self.tmp), extra],
            capture_output=True, text=True, env=env, cwd=self.tmp, check=True,
        )
        return result.stdout

    def test_temp_config_files_are_kept(self):
        out = self.run_driver()
        for name in WALLET_NAMES:
            self.assertIn(f"WALLET:{self.target / name}", out, name)
        self.assertIn("KEEP:The wallet mnemonics (4 files", out)
        wallet_dir = Path(re.search(r"WALLET_DIR:(.*)", out).group(1))
        for name in WALLET_NAMES:
            self.assertTrue((wallet_dir / name).is_file(), name)
            self.assertFalse((self.target / name).exists(), name)

    def test_temp_config_files_are_removed_only_with_delete_wallets(self):
        out = self.run_driver("--delete-wallets")
        self.assertIn("REMOVE:THE WALLET MNEMONICS: ", out)
        self.assertIn(str(self.target / ".config-abc123.yaml"), out)
        self.assertNotIn("KEEP:The wallet mnemonics", out)
        # Not moved out: they go with the root.
        for name in WALLET_NAMES:
            self.assertTrue((self.target / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()

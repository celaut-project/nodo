"""install.sh must not move a `main.*` path the operator relocated (issue #606).

`nodo update` re-runs `install.sh`, which used to rewrite all seven `main.*` keys to
`$TARGET_DIR/storage/...`: a relocated STORAGE / DATABASE_FILE then silently pointed
the daemon at a new, empty database. These tests run the real `sync_config_main_paths`
(cut out of install.sh, never the installer itself) against a temp config.yaml.
"""

import os
import subprocess
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _functions() -> str:
    """The text of escape_for_sed through sync_config_main_paths, from install.sh."""
    with open(os.path.join(_ROOT, "install.sh")) as handle:
        text = handle.read()
    start = text.index("escape_for_sed() {")
    end = text.index('if [ "$USE_LOCAL_SOURCE" = true ]', start)
    return text[start:end]


def _example() -> str:
    with open(os.path.join(_ROOT, "config.example.yaml")) as handle:
        return handle.read()


class SyncConfigMainPathsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = os.path.join(self._tmp.name, "nodo")
        os.makedirs(self.target)
        self.config = os.path.join(self.target, "config.yaml")

    def _write(self, text: str = None):
        with open(self.config, "w") as handle:
            handle.write(_example() if text is None else text)

    def _sync(self) -> str:
        script = _functions() + "\nsync_config_main_paths\n"
        result = subprocess.run(
            ["bash", "-c", script],
            env={"PATH": os.environ["PATH"], "TARGET_DIR": self.target},
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def _main(self) -> dict:
        values = {}
        in_main = False
        with open(self.config) as handle:
            for line in handle:
                if line.startswith("main:"):
                    in_main = True
                elif in_main and line.startswith("  "):
                    key, _, value = line.strip().partition(":")
                    values[key] = value.strip().strip('"')
                elif in_main:
                    break
        return values

    def _relocate(self, **overrides):
        text = _example()
        for key, value in overrides.items():
            lines = [
                f'  {key}: "{value}"' if line.startswith(f"  {key}:") else line
                for line in text.splitlines()
            ]
            text = "\n".join(lines) + "\n"
        self._write(text)

    def test_fresh_example_config_is_rewritten_to_the_target_dir(self):
        self._write()
        self._sync()
        storage = f"{self.target}/storage"
        self.assertEqual(self._main(), {
            "MAIN_DIR": self.target,
            "STORAGE": storage,
            "CACHE": f"{storage}/__cache__/",
            "REGISTRY": f"{storage}/__registry__/",
            "METADATA_REGISTRY": f"{storage}/__metadata__/",
            "BLOCKDIR": f"{storage}/__block__/",
            "DATABASE_FILE": f"{storage}/database.sqlite",
        })

    def test_relocated_storage_database_and_blockdir_are_kept(self):
        self._relocate(
            STORAGE="/data/nodo-storage",
            DATABASE_FILE="/data/nodo-storage/database.sqlite",
            BLOCKDIR="/data/blocks/",
        )
        out = self._sync()
        main = self._main()
        self.assertEqual(main["STORAGE"], "/data/nodo-storage")
        self.assertEqual(main["DATABASE_FILE"], "/data/nodo-storage/database.sqlite")
        self.assertEqual(main["BLOCKDIR"], "/data/blocks/")
        self.assertIn("Keeping main.STORAGE", out)
        self.assertIn("Keeping main.BLOCKDIR", out)

    def test_placeholders_follow_a_relocated_storage(self):
        self._relocate(STORAGE="/data/nodo-storage")
        self._sync()
        main = self._main()
        self.assertEqual(main["CACHE"], "/data/nodo-storage/__cache__/")
        self.assertEqual(main["REGISTRY"], "/data/nodo-storage/__registry__/")
        self.assertEqual(main["DATABASE_FILE"], "/data/nodo-storage/database.sqlite")
        self.assertNotIn("${", "".join(main.values()))

    def test_empty_value_gets_the_default(self):
        self._relocate(CACHE="")
        self._sync()
        self.assertEqual(self._main()["CACHE"], f"{self.target}/storage/__cache__/")

    def test_a_value_equal_to_the_default_is_silent(self):
        self._relocate(STORAGE=f"{self.target}/storage")
        self.assertNotIn("Keeping", self._sync())

    def test_second_run_changes_nothing(self):
        self._relocate(STORAGE="/data/nodo-storage", DATABASE_FILE="/data/db/n.sqlite")
        self._sync()
        with open(self.config) as handle:
            first = handle.read()
        self._sync()
        with open(self.config) as handle:
            self.assertEqual(handle.read(), first)

    def test_second_run_on_a_fresh_config_changes_nothing(self):
        self._write()
        self._sync()
        with open(self.config) as handle:
            first = handle.read()
        self._sync()
        with open(self.config) as handle:
            self.assertEqual(handle.read(), first)

    def test_main_dir_always_follows_the_target_dir(self):
        self._relocate(MAIN_DIR="/somewhere/else")
        self._sync()
        self.assertEqual(self._main()["MAIN_DIR"], self.target)


if __name__ == "__main__":
    unittest.main()

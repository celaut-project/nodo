"""`nodo update` pulls only when compat.json says the node can run the new code (#518).

Each case builds an origin and a clone in temporary directories, publishes one commit to
the origin, and asks `check_update` / `update_command` what they make of it. When the
command refuses, the clone's tree and its storage/ directory must be exactly as they were.
"""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.commands import update
    from src.utils import compat
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

MANIFEST = {
    "celaut_proto": "a" * 64,
    "config_keys": ["main.MAIN_DIR", "main.STORAGE"],
    "database": "b" * 64,
    "storage_format": 1,
}


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def write(root, path, text):
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(text)


def read(root, path):
    with open(os.path.join(root, path)) as f:
        return f.read()


def tree_digest(root):
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            path = os.path.join(base, name)
            digest.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as f:
                digest.update(f.read())
    return digest.hexdigest()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class UpdateCompatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.origin = os.path.join(tmp.name, "origin.git")
        self.seed = os.path.join(tmp.name, "seed")
        self.clone = os.path.join(tmp.name, "clone")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", self.origin], check=True)
        subprocess.run(["git", "init", "-q", "-b", "dev", self.seed], check=True)
        write(self.seed, "compat.json", compat.dump_manifest(MANIFEST))
        write(self.seed, "code.py", "v1\n")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-qm", "v1")
        git(self.seed, "remote", "add", "origin", self.origin)
        git(self.seed, "push", "-q", "origin", "dev")
        subprocess.run(["git", "clone", "-q", "-b", "dev", self.origin, self.clone], check=True)
        write(self.clone, "storage/__block__/b", "block\n")  # untracked, like a real node's data

    def publish(self, **changes):
        manifest = dict(MANIFEST, **changes)
        write(self.seed, "compat.json", compat.dump_manifest(manifest))
        write(self.seed, "code.py", "v2\n")
        git(self.seed, "commit", "-qam", "v2")
        git(self.seed, "push", "-q", "origin", "dev")

    def refused(self, status, **changes):
        self.publish(**changes)
        before, head = tree_digest(self.clone), git_head(self.clone)
        verdict = update.check_update(self.clone)
        self.assertEqual(verdict.status, status)
        self.assertFalse(update.update_command(self.clone))
        self.assertEqual(tree_digest(self.clone), before)
        self.assertEqual(git_head(self.clone), head)
        return verdict

    def test_up_to_date(self):
        self.assertEqual(update.check_update(self.clone).status, update.UP_TO_DATE)
        self.assertTrue(update.update_command(self.clone))

    def test_nothing_changed_updates(self):
        self.publish()
        self.assertEqual(update.check_update(self.clone).status, update.COMPATIBLE)
        self.assertTrue(update.update_command(self.clone, check_only=True))
        self.assertEqual(read(self.clone, "code.py"), "v1\n")  # --check pulls nothing
        verdict = update.check_update(self.clone)
        self.assertTrue(update.apply_update(self.clone, verdict, install_command=["true"]))
        self.assertEqual(read(self.clone, "code.py"), "v2\n")
        self.assertTrue(os.path.exists(os.path.join(self.clone, "storage/__block__/b")))

    def test_proto_changed(self):
        v = self.refused(update.INCOMPATIBLE, celaut_proto="c" * 64)
        self.assertEqual(v.changed, ["celaut_proto"])

    def test_config_key_added(self):
        v = self.refused(update.INCOMPATIBLE, config_keys=MANIFEST["config_keys"] + ["main.NEW"])
        self.assertEqual(v.changed, ["config_keys"])
        self.assertIn("added:   main.NEW", "\n".join(v.lines))

    def test_config_key_removed(self):
        v = self.refused(update.INCOMPATIBLE, config_keys=["main.MAIN_DIR"])
        self.assertIn("removed: main.STORAGE", "\n".join(v.lines))

    def test_database_changed(self):
        self.assertEqual(self.refused(update.INCOMPATIBLE, database="d" * 64).changed, ["database"])

    def test_storage_format_changed(self):
        self.assertEqual(self.refused(update.INCOMPATIBLE, storage_format=2).changed, ["storage_format"])

    def test_local_changes_stop_the_update_unless_forced(self):
        write(self.clone, "code.py", "mine\n")
        verdict = self.refused(update.DIRTY)
        self.assertEqual(verdict.dirty, ["code.py"])
        self.assertEqual(read(self.clone, "code.py"), "mine\n")

        forced = update.check_update(self.clone, force=True)
        self.assertEqual(forced.status, update.COMPATIBLE)
        update.restore_local_changes(self.clone)
        self.assertEqual(update.local_changes(self.clone), [])

    def test_local_changes_listing_reads_renames_and_spaces(self):
        write(self.clone, "a b.txt", "x\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-qm", "spaced")
        git(self.clone, "mv", "a b.txt", "c d.txt")
        self.assertEqual(update.local_changes(self.clone), ["c d.txt"])

    def test_missing_manifest_is_unknown(self):
        git(self.seed, "rm", "-q", "compat.json")
        write(self.seed, "code.py", "v2\n")
        git(self.seed, "commit", "-qam", "drop manifest")
        git(self.seed, "push", "-q", "origin", "dev")
        self.assertEqual(update.check_update(self.clone).status, update.UNKNOWN)

    def test_failed_install_goes_back(self):
        self.publish()
        verdict = update.check_update(self.clone)
        self.assertFalse(update.apply_update(self.clone, verdict, install_command=["false"]))
        self.assertEqual(git_head(self.clone), verdict.installed)
        self.assertEqual(read(self.clone, "code.py"), "v1\n")


def git_head(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ManifestTests(unittest.TestCase):
    def test_committed_manifest_matches_the_sources(self):
        with open(os.path.join(compat.ROOT, compat.MANIFEST_FILE)) as f:
            self.assertEqual(json.load(f), compat.compute_manifest())

    def test_flatten_keys(self):
        self.assertEqual(compat.flatten_keys({"a": {"b": 1, "c": {"d": 2}}, "e": []}), ["a.b", "a.c.d", "e"])


if __name__ == "__main__":
    unittest.main()

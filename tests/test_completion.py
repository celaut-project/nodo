import os
import sqlite3
import tempfile
import unittest

from src.commands import completion

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]


class CommandCatalogueTests(unittest.TestCase):
    def test_every_id_command_is_a_known_command(self):
        for command in (
            completion.SERVICE_COMMANDS
            + completion.INSTANCE_COMMANDS
            + completion.PEER_COMMANDS
        ):
            self.assertIn(command, completion.COMMANDS, command)

    def test_commands_kind_needs_no_config(self):
        # Must resolve without touching config.yaml / the database.
        self.assertEqual(completion.candidates("commands"), completion.COMMANDS)
        self.assertIn("execute", completion.candidates("commands"))
        self.assertEqual(completion.candidates("daemon"), completion.DAEMON_SUBCOMMANDS)

    def test_command_and_arg_lists_are_disjoint_roles(self):
        # A command can only carry one kind of first-arg completion.
        service = set(completion.SERVICE_COMMANDS)
        instance = set(completion.INSTANCE_COMMANDS)
        peer = set(completion.PEER_COMMANDS)
        self.assertFalse(service & instance)
        self.assertFalse(service & peer)
        self.assertFalse(instance & peer)


class DatabaseCandidateTests(unittest.TestCase):
    def _build_db(self, path):
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE local_instances (id TEXT PRIMARY KEY, name TEXT)"
            )
            conn.executemany(
                "INSERT INTO local_instances (id, name) VALUES (?, ?)",
                [("inst-aaa", "alpha"), ("inst-bbb", "beta"), ("inst-ccc", None)],
            )
            conn.execute("CREATE TABLE peer (id TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT INTO peer (id) VALUES (?)", [("peer-1",), ("peer-2",)]
            )
            conn.commit()
        finally:
            conn.close()

    def test_instance_candidates_include_ids_and_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "db.sqlite")
            self._build_db(db)
            result = completion.instance_candidates(db)
            self.assertIn("inst-aaa", result)
            self.assertIn("alpha", result)
            self.assertIn("inst-ccc", result)  # id present even with NULL name

    def test_peer_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "db.sqlite")
            self._build_db(db)
            self.assertEqual(sorted(completion.peer_candidates(db)), ["peer-1", "peer-2"])

    def test_missing_database_is_safe(self):
        self.assertEqual(completion.instance_candidates("/no/such/db.sqlite"), [])
        self.assertEqual(completion.peer_candidates(None), [])


class ServiceCandidateTests(unittest.TestCase):
    def test_missing_registry_is_safe(self):
        self.assertEqual(completion.service_candidates(None, None), [])
        self.assertEqual(completion.service_candidates("/no/such/registry", None), [])

    @unittest.skipIf(IMPORT_ERROR is not None, f"Missing protobuf: {IMPORT_ERROR}")
    def test_service_candidates_include_ids_and_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "registry")
            metadata = os.path.join(tmp, "metadata")
            os.makedirs(registry)
            os.makedirs(metadata)
            for service_id, tag in [("svc-aaa", "webserver"), ("svc-bbb", "")]:
                open(os.path.join(registry, service_id), "wb").close()
                md = celaut.Metadata()
                if tag:
                    md.hashtag.tag.append(tag)
                with open(os.path.join(metadata, service_id), "wb") as handle:
                    handle.write(md.SerializeToString())
            result = completion.service_candidates(registry, metadata)
            self.assertIn("svc-aaa", result)
            self.assertIn("webserver", result)  # tag surfaced
            self.assertIn("svc-bbb", result)  # untagged service still completes

    @unittest.skipIf(IMPORT_ERROR is not None, f"Missing protobuf: {IMPORT_ERROR}")
    def test_shared_tags_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "registry")
            metadata = os.path.join(tmp, "metadata")
            os.makedirs(registry)
            os.makedirs(metadata)
            # Three distinct services that all carry the same tag.
            for service_id in ("svc-1", "svc-2", "svc-3"):
                open(os.path.join(registry, service_id), "wb").close()
                md = celaut.Metadata()
                md.hashtag.tag.append("packer")
                with open(os.path.join(metadata, service_id), "wb") as handle:
                    handle.write(md.SerializeToString())
            result = completion.service_candidates(registry, metadata)
            self.assertEqual(result.count("packer"), 1)  # tag listed once
            for service_id in ("svc-1", "svc-2", "svc-3"):
                self.assertIn(service_id, result)  # every id still present


class ScriptGenerationTests(unittest.TestCase):
    def test_bash_script_bakes_paths_and_commands(self):
        script = completion.bash_script("/opt/nodo", "/opt/nodo/venv/bin/python")
        self.assertIn('nodo_py="/opt/nodo/venv/bin/python"', script)
        self.assertIn('nodo_dir="/opt/nodo"', script)
        self.assertIn('"$nodo_dir/src/commands/completion.py"', script)
        self.assertIn("complete -o default -F _nodo_completion nodo", script)
        self.assertIn("execute", script)  # a service command routed
        self.assertIn("kill", script)  # an instance command routed

    def test_zsh_script_bakes_paths_and_header(self):
        script = completion.zsh_script("/opt/nodo", "/opt/nodo/venv/bin/python")
        self.assertTrue(script.lstrip().startswith("#compdef nodo"))
        self.assertIn("/opt/nodo/venv/bin/python", script)
        self.assertIn("_describe", script)

    def test_install_targets_differ_for_user_and_system(self):
        user = completion._completion_targets(user=True)
        system = completion._completion_targets(user=False)
        self.assertNotEqual(user["bash"], system["bash"])
        self.assertTrue(user["bash"].endswith("/completions/nodo"))


if __name__ == "__main__":
    unittest.main()

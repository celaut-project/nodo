import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
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
            + completion.PATH_COMMANDS
            + completion.SECOND_ARG_PATH_COMMANDS
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
        path = set(completion.PATH_COMMANDS)
        self.assertFalse(service & instance)
        self.assertFalse(service & peer)
        self.assertFalse(instance & peer)
        # A path-first command is never also an id-first command.
        self.assertFalse(path & (service | instance | peer))

    def test_second_arg_path_commands_take_an_id_first(self):
        # `export <service> <path>`: the command's first arg is still an id, so it
        # must live in one of the id lists (services here), not PATH_COMMANDS.
        first_arg_id = (
            set(completion.SERVICE_COMMANDS)
            | set(completion.INSTANCE_COMMANDS)
            | set(completion.PEER_COMMANDS)
        )
        for command in completion.SECOND_ARG_PATH_COMMANDS:
            self.assertIn(command, first_arg_id, command)
            self.assertNotIn(command, completion.PATH_COMMANDS, command)


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
        self.assertIn("complete -o bashdefault -o default -F _nodo_completion nodo", script)
        self.assertIn("execute", script)  # a service command routed
        self.assertIn("kill", script)  # an instance command routed
        # Config must be loaded from the install dir, not the user's cwd.
        self.assertIn('NODO_COMPLETION_DIR="$nodo_dir"', script)
        # Directory-aware path completion + path-first / second-arg-path commands.
        self.assertIn("compopt -o filenames", script)
        self.assertIn("import", script)  # a path-first command routed
        self.assertIn("_nodo_paths", script)

    def test_zsh_script_bakes_paths_and_header(self):
        script = completion.zsh_script("/opt/nodo", "/opt/nodo/venv/bin/python")
        self.assertTrue(script.lstrip().startswith("#compdef nodo"))
        self.assertIn("/opt/nodo/venv/bin/python", script)
        self.assertIn("_describe", script)
        self.assertIn('NODO_COMPLETION_DIR="$nodo_dir"', script)
        self.assertIn("_files", script)  # path completion

    def test_install_targets_differ_for_user_and_system(self):
        user = completion._completion_targets(user=True)
        system = completion._completion_targets(user=False)
        self.assertNotEqual(user["bash"], system["bash"])
        self.assertTrue(user["bash"].endswith("/completions/nodo"))


class ConfigDirResolutionTests(unittest.TestCase):
    """The shell runs the helper from the user's cwd; id completion must still
    resolve by loading config.yaml from the nodo install dir (NODO_COMPLETION_DIR),
    not from that cwd. This is the regression the fix addresses."""

    _COMPLETION_PY = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src",
        "commands",
        "completion.py",
    )

    def _write_nodo_dir(self) -> str:
        root = tempfile.mkdtemp(prefix="nodo-completion-root-")
        storage = os.path.join(root, "storage")
        os.makedirs(os.path.join(storage, "__registry__", "svc-xyz-123"))
        with open(os.path.join(root, "config.yaml"), "w", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    f"""\
                    main:
                      MAIN_DIR: "{root}"
                      STORAGE: "{storage}"
                      REGISTRY: "${{main.STORAGE}}/__registry__/"
                      METADATA_REGISTRY: "${{main.STORAGE}}/__metadata__/"
                      DATABASE_FILE: "${{main.STORAGE}}/database.sqlite"
                    """
                )
            )
        return root

    def _run_list(self, kind, cwd, env):
        return subprocess.run(
            [sys.executable, self._COMPLETION_PY, "list", kind],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_ids_resolve_from_any_cwd(self):
        nodo_dir = self._write_nodo_dir()
        elsewhere = tempfile.mkdtemp(prefix="nodo-completion-cwd-")  # no config.yaml
        env = dict(os.environ, NODO_COMPLETION_DIR=nodo_dir)
        result = self._run_list("services", cwd=elsewhere, env=env)
        if result.returncode != 0 and "ModuleNotFoundError" in result.stderr:
            self.skipTest(f"runtime deps unavailable: {result.stderr.strip()[:200]}")
        self.assertIn("svc-xyz-123", result.stdout, result.stderr)

    def test_commands_list_needs_no_config(self):
        # `list commands` must never depend on config resolution.
        elsewhere = tempfile.mkdtemp(prefix="nodo-completion-cwd-")
        result = self._run_list("commands", cwd=elsewhere, env=dict(os.environ))
        if result.returncode != 0 and "ModuleNotFoundError" in result.stderr:
            self.skipTest(f"runtime deps unavailable: {result.stderr.strip()[:200]}")
        self.assertIn("execute", result.stdout.split())


if __name__ == "__main__":
    unittest.main()

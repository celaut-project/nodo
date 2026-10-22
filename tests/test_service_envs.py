"""The env vars a service asks for, and how ``nodo execute`` fills in the missing ones."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.utils import keyvalue, service_envs
    from src.utils.registry_errors import ServiceNotInRegistry
    from src.commands import execute as execute_cmd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _service(declared=(), networks=()):
    """A service declaring ``declared`` [(name, tags, prose)] and ``networks`` [(tags, formal)]."""
    service = celaut.Service()
    for name, tags, prose in declared:
        keyvalue.set_value(
            service.container.environment_variables,
            name,
            celaut.DataFormat(tags=tags, prose=prose),
        )
    for tags, formal in networks:
        service.network.add(tags=tags, formal=formal)
    return service


ERGO = (["pow:ergo"], b"pow.block_id=${BLOCK}\npow.chain=ergo")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EnvSpecsTests(unittest.TestCase):
    def test_declared_vars_are_listed_with_their_format(self):
        specs = service_envs.env_specs(_service(declared=[("LOG", ["text"], "log level")]))
        self.assertEqual([s.name for s in specs], ["LOG"])
        self.assertEqual(specs[0].tags, ("text",))
        self.assertEqual(specs[0].prose, "log level")

    def test_a_placeholder_does_not_repeat_a_declared_var(self):
        specs = service_envs.env_specs(_service(
            declared=[("LOG", [], ""), ("BLOCK", ["hex"], "chain tip")], networks=[ERGO],
        ))
        self.assertEqual([s.name for s in specs], ["BLOCK", "LOG"])
        self.assertEqual(specs[0].prose, "chain tip")

    def test_undeclared_placeholder_is_still_listed(self):
        specs = service_envs.env_specs(_service(networks=[ERGO]))
        self.assertEqual([s.name for s in specs], ["BLOCK"])

    def test_declared_vars_come_before_undeclared_placeholders(self):
        specs = service_envs.env_specs(_service(declared=[("A", [], ""), ("C", [], "")], networks=[ERGO]))
        self.assertEqual([s.name for s in specs], ["A", "C", "BLOCK"])

    def test_missing_envs(self):
        specs = service_envs.env_specs(_service(declared=[("A", [], ""), ("B", [], "")]))
        self.assertEqual([s.name for s in service_envs.missing_envs(specs, {"A": ""})], ["B"])

    def test_json_shape(self):
        spec = service_envs.EnvSpec(name="BLOCK", tags=("hex",), prose="p")
        self.assertEqual(spec.to_json(), {"name": "BLOCK", "tags": ["hex"], "prose": "p"})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CompleteEnvsTests(unittest.TestCase):
    def setUp(self):
        self.service = _service(declared=[("LOG", [], "log level")], networks=[ERGO])
        loader = patch.object(execute_cmd, "load_service_from_disk", return_value=self.service)
        loader.start()
        self.addCleanup(loader.stop)

    def _complete(self, envs, interactive, answers=()):
        out = io.StringIO()
        with patch("builtins.input", side_effect=list(answers)) as ask, redirect_stdout(out):
            result = execute_cmd.complete_envs("svc", envs, interactive=interactive)
        return result, out.getvalue(), ask

    def test_nothing_missing_asks_nothing(self):
        result, out, ask = self._complete({"BLOCK": "b", "LOG": "x"}, interactive=True)
        self.assertEqual(result, {"BLOCK": "b", "LOG": "x"})
        self.assertEqual(out, "")
        ask.assert_not_called()

    def test_non_interactive_reports_the_missing_vars_and_goes_on(self):
        result, out, ask = self._complete({"LOG": "x"}, interactive=False)
        self.assertEqual(result, {"LOG": "x"})
        self.assertIn("not set: BLOCK", out)
        ask.assert_not_called()

    def test_interactive_asks_one_by_one_only_for_the_missing_vars(self):
        result, out, ask = self._complete({"LOG": "x"}, interactive=True, answers=["tip"])
        self.assertEqual(result, {"LOG": "x", "BLOCK": "tip"})
        self.assertEqual(ask.call_count, 1)
        self.assertIn("[1/1]", out)

    def test_interactive_numbers_each_question(self):
        _, out, ask = self._complete({}, interactive=True, answers=["a", "b"])
        self.assertEqual(ask.call_count, 2)
        self.assertIn("[1/2]", out)
        self.assertIn("[2/2]", out)

    def test_enter_leaves_a_var_unset_and_says_so(self):
        result, out, _ = self._complete({}, interactive=True, answers=["", "debug"])
        self.assertEqual(result, {"BLOCK": "debug"})
        self.assertIn("Not set: LOG", out)

    def test_values_are_never_printed(self):
        _, out, _ = self._complete({}, interactive=True, answers=["s3cr3t", "t0k3n"])
        self.assertNotIn("s3cr3t", out)
        self.assertNotIn("t0k3n", out)

    def test_ctrl_d_cancels(self):
        result, out, _ = self._complete({}, interactive=True, answers=[EOFError()])
        self.assertIsNone(result)
        self.assertIn("Cancelled", out)

    def test_unreadable_spec_leaves_envs_as_given(self):
        with patch.object(execute_cmd, "load_service_from_disk", side_effect=ServiceNotInRegistry("x")):
            result = execute_cmd.complete_envs("svc", {"A": "1"}, interactive=False)
        self.assertEqual(result, {"A": "1"})

    def test_execute_launches_with_what_was_given(self):
        with patch.object(execute_cmd, "resolve_service_hash", return_value="svc"), \
                patch.object(execute_cmd, "launch_via_gateway", return_value=None) as launch, \
                redirect_stdout(io.StringIO()):
            execute_cmd.execute("svc", envs={}, check_envs=True, ask_envs=False)
        launch.assert_called_once()

    def test_execute_without_check_launches_as_before(self):
        with patch.object(execute_cmd, "resolve_service_hash", return_value="svc"), \
                patch.object(execute_cmd, "launch_via_gateway", return_value=None) as launch:
            execute_cmd.execute("svc", envs={})
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

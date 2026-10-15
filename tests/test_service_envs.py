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
    def test_declared_vars_are_optional(self):
        specs = service_envs.env_specs(_service(declared=[("LOG", ["text"], "log level")]))
        self.assertEqual([s.name for s in specs], ["LOG"])
        self.assertFalse(specs[0].required)
        self.assertEqual(specs[0].tags, ("text",))
        self.assertEqual(specs[0].prose, "log level")

    def test_network_placeholder_makes_a_declared_var_required(self):
        specs = service_envs.env_specs(_service(
            declared=[("LOG", [], ""), ("BLOCK", ["hex"], "chain tip")], networks=[ERGO],
        ))
        by_name = {s.name: s for s in specs}
        self.assertTrue(by_name["BLOCK"].required)
        self.assertEqual(by_name["BLOCK"].networks, ("pow:ergo",))
        self.assertEqual(by_name["BLOCK"].prose, "chain tip")
        self.assertFalse(by_name["LOG"].required)

    def test_undeclared_placeholder_is_still_listed_as_required(self):
        specs = service_envs.env_specs(_service(networks=[ERGO]))
        self.assertEqual([(s.name, s.required) for s in specs], [("BLOCK", True)])

    def test_required_vars_come_first(self):
        specs = service_envs.env_specs(_service(
            declared=[("A", [], ""), ("BLOCK", [], ""), ("C", [], "")], networks=[ERGO],
        ))
        self.assertEqual([s.name for s in specs], ["BLOCK", "A", "C"])

    def test_missing_envs(self):
        specs = service_envs.env_specs(_service(declared=[("A", [], ""), ("B", [], "")]))
        self.assertEqual([s.name for s in service_envs.missing_envs(specs, {"A": ""})], ["B"])

    def test_required_var_needs_a_usable_value(self):
        spec = service_envs.EnvSpec(name="BLOCK", networks=("pow:ergo",))
        self.assertFalse(service_envs.is_answered(spec, {}))
        self.assertFalse(service_envs.is_answered(spec, {"BLOCK": ""}))
        self.assertFalse(service_envs.is_answered(spec, {"BLOCK": "a\nb"}))
        self.assertTrue(service_envs.is_answered(spec, {"BLOCK": "abc"}))

    def test_json_shape(self):
        spec = service_envs.EnvSpec(name="BLOCK", tags=("hex",), prose="p", networks=("pow:ergo",))
        self.assertEqual(spec.to_json(), {
            "name": "BLOCK", "tags": ["hex"], "prose": "p",
            "required": True, "networks": ["pow:ergo"],
        })


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

    def test_non_interactive_aborts_on_a_missing_required_var(self):
        result, out, ask = self._complete({"LOG": "x"}, interactive=False)
        self.assertIsNone(result)
        self.assertIn("Missing required env var: BLOCK", out)
        ask.assert_not_called()

    def test_non_interactive_only_warns_about_optional_vars(self):
        result, out, _ = self._complete({"BLOCK": "b"}, interactive=False)
        self.assertEqual(result, {"BLOCK": "b"})
        self.assertIn("Optional env vars not given, so not set: LOG", out)

    def test_interactive_asks_again_until_a_required_var_has_a_value(self):
        result, out, ask = self._complete({}, interactive=True, answers=["", "tip", ""])
        self.assertEqual(result, {"BLOCK": "tip"})
        self.assertEqual(ask.call_count, 3)
        self.assertIn("BLOCK is required", out)

    def test_interactive_keeps_an_optional_answer(self):
        result, _, _ = self._complete({"BLOCK": "b"}, interactive=True, answers=["debug"])
        self.assertEqual(result, {"BLOCK": "b", "LOG": "debug"})

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

    def test_execute_does_not_launch_when_a_required_var_is_missing(self):
        with patch.object(execute_cmd, "resolve_service_hash", return_value="svc"), \
                patch.object(execute_cmd, "launch_via_gateway") as launch, \
                redirect_stdout(io.StringIO()):
            execute_cmd.execute("svc", envs={}, check_envs=True, ask_envs=False)
        launch.assert_not_called()

    def test_execute_without_check_launches_as_before(self):
        with patch.object(execute_cmd, "resolve_service_hash", return_value="svc"), \
                patch.object(execute_cmd, "launch_via_gateway", return_value=None) as launch:
            execute_cmd.execute("svc", envs={})
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

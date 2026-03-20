import unittest

IMPORT_ERROR = None
try:
    from src.virtualizers.entry_path import resolve_entrypoint_path
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    resolve_entrypoint_path = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DockerExecuteHelpersTests(unittest.TestCase):
    def test_resolve_entrypoint_accepts_absolute_single_value(self):
        self.assertEqual(resolve_entrypoint_path(["/tiny-service"]), "/tiny-service")

    def test_resolve_entrypoint_normalizes_relative_single_value(self):
        self.assertEqual(resolve_entrypoint_path(["tiny-service"]), "/tiny-service")

    def test_resolve_entrypoint_accepts_segmented_values(self):
        self.assertEqual(
            resolve_entrypoint_path(["usr", "local", "bin", "tiny-service"]),
            "/usr/local/bin/tiny-service",
        )

    def test_resolve_entrypoint_accepts_segmented_slash_values(self):
        self.assertEqual(
            resolve_entrypoint_path(["/usr/local", "bin/tiny-service"]),
            "/usr/local/bin/tiny-service",
        )

    def test_resolve_entrypoint_rejects_empty(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            resolve_entrypoint_path([])

    def test_resolve_entrypoint_rejects_dot_segments(self):
        with self.assertRaisesRegex(ValueError, "must not contain '\\.' or '\\.\\.' segments"):
            resolve_entrypoint_path(["usr", "..", "tiny-service"])

    def test_resolve_entrypoint_rejects_spaces(self):
        with self.assertRaisesRegex(ValueError, "without spaces"):
            resolve_entrypoint_path(["tiny service"])

    def test_resolve_entrypoint_rejects_cli_arguments(self):
        with self.assertRaisesRegex(ValueError, "not CLI arguments"):
            resolve_entrypoint_path(["/bin/server", "--flag"])


if __name__ == "__main__":
    unittest.main()

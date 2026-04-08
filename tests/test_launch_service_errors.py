import unittest

IMPORT_ERROR = None
try:
    from src.gateway.launcher.launch_service import _format_launch_failure
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    _format_launch_failure = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LaunchServiceErrorFormattingTests(unittest.TestCase):
    def test_reports_local_preflight_failure_when_no_attempts_run(self):
        message = _format_launch_failure(
            service_id="svc123",
            launch_failures=[],
            local_preflight_failure="Unsupported architecture 'linux/arm64'.",
        )

        self.assertIn("Unable to launch service svc123.", message)
        self.assertIn("local preflight: Unsupported architecture 'linux/arm64'.", message)

    def test_reports_peer_attempt_failures(self):
        message = _format_launch_failure(
            service_id="svc123",
            launch_failures=[
                "peer-a: RuntimeError: timed out",
                "local: RuntimeError: image build failed",
            ],
        )

        self.assertIn("peer-a: RuntimeError: timed out", message)
        self.assertIn("local: RuntimeError: image build failed", message)

    def test_reports_absence_of_any_executor_when_no_details_are_available(self):
        message = _format_launch_failure(
            service_id="svc123",
            launch_failures=[],
        )

        self.assertIn("no eligible local executor or peer was available", message)


if __name__ == "__main__":
    unittest.main()

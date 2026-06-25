import tempfile
import unittest
from pathlib import Path

from src.commands.doctor import _render_service_template


class DoctorServiceTemplateRenderTests(unittest.TestCase):
    def test_renders_all_systemd_template_runtime_placeholders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            main_dir = Path(tmpdir)
            (main_dir / "config.yaml").write_text(
                """
dependencies:
  java:
    JAVA_HOME: "${main.MAIN_DIR}/custom/java"
  python:
    RUNTIME_BIN: "${main.MAIN_DIR}/custom/python/bin/python3"
    VENV_BIN: "${main.MAIN_DIR}/custom/venv/bin/python"
""",
                encoding="utf-8",
            )

            template = Path("bash/nodo.service.template").read_text(encoding="utf-8")
            rendered = _render_service_template(template, str(main_dir))

        self.assertNotIn("{{", rendered)
        self.assertIn(f"Environment=JAVA_HOME={main_dir}/custom/java", rendered)
        self.assertIn(f"{main_dir}/custom/python/bin", rendered)
        self.assertIn(f"exec {main_dir}/custom/venv/bin/python {main_dir}/nodo.py serve", rendered)

    def test_rejects_unresolved_systemd_template_placeholders(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_PLACEHOLDER"):
            _render_service_template("ExecStart={{UNKNOWN_PLACEHOLDER}}\n", "/nodo")


if __name__ == "__main__":
    unittest.main()

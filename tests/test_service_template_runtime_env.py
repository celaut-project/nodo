import unittest
from pathlib import Path


class ServiceTemplateRuntimeEnvTests(unittest.TestCase):
    def test_service_template_exports_local_java_and_path(self):
        content = Path("bash/nodo.service.template").read_text(encoding="utf-8")
        self.assertIn("Environment=JAVA_HOME={{JAVA_HOME}}", content)
        self.assertIn(
            "Environment=PATH={{JAVA_HOME}}/bin:{{PYTHON_RUNTIME_BIN_DIR}}:{{MAIN_DIR}}/bin:",
            content,
        )

    def test_service_template_uses_venv_python_binary(self):
        content = Path("bash/nodo.service.template").read_text(encoding="utf-8")
        self.assertIn("exec {{PYTHON_VENV_BIN}} {{MAIN_DIR}}/nodo.py serve", content)


if __name__ == "__main__":
    unittest.main()

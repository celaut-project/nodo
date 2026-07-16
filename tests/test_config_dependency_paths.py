import unittest
from pathlib import Path


class ConfigDependencyPathsTests(unittest.TestCase):
    def test_config_example_declares_dependency_paths(self):
        content = Path("config.example.yaml").read_text(encoding="utf-8")
        self.assertIn("dependencies:", content)
        self.assertIn("java:", content)
        self.assertIn("JAVA_HOME:", content)
        self.assertIn("python:", content)
        self.assertIn("RUNTIME_BIN:", content)
        self.assertIn("VENV_BIN:", content)
        self.assertIn("yq:", content)
        self.assertIn("BIN:", content)
        # Docker dependency paths were removed (no local Docker); packing is
        # delegated to the packer-service. Its service id lives in ONE place — the
        # `core_services` "packer" entry — so the packer section carries no
        # PACKER_SERVICE_ID key at all (see issue #135), not even as an env-var
        # override. The packer section keeps only the out-of-band PACKER_SERVICE_URL
        # override and the PACKER_SOURCE_URL download source.
        self.assertNotIn("PACKER_SERVICE_ID", content)
        self.assertIn("PACKER_SERVICE_URL:", content)
        self.assertIn("PACKER_SOURCE_URL:", content)


if __name__ == "__main__":
    unittest.main()

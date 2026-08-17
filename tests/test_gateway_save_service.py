import os
import tempfile
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    import src.gateway.utils as gateway_utils
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    gateway_utils = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SaveServiceTests(unittest.TestCase):
    def test_existing_metadata_does_not_mask_missing_service_contents(self):
        service_hash = "abc123"
        metadata = celaut.Metadata()

        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "registry") + os.sep
            metadata_registry = os.path.join(tmp, "metadata") + os.sep
            os.makedirs(registry)
            os.makedirs(metadata_registry)

            source = os.path.join(tmp, "incoming")
            os.makedirs(source)
            with open(os.path.join(metadata_registry, service_hash), "wb") as f:
                f.write(b"stale")

            with patch.object(gateway_utils, "REGISTRY", registry), patch.object(
                gateway_utils, "METADATA_REGISTRY", metadata_registry
            ):
                saved = gateway_utils.save_service(
                    metadata=metadata,
                    service_dir=source,
                    service_hash=service_hash,
                )

            self.assertTrue(saved)
            self.assertTrue(os.path.isdir(os.path.join(registry, service_hash)))

    def test_failed_move_does_not_write_metadata(self):
        service_hash = "abc123"
        metadata = celaut.Metadata()

        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "registry") + os.sep
            metadata_registry = os.path.join(tmp, "metadata") + os.sep
            os.makedirs(registry)
            os.makedirs(metadata_registry)

            with patch.object(gateway_utils, "REGISTRY", registry), patch.object(
                gateway_utils, "METADATA_REGISTRY", metadata_registry
            ), patch.object(
                gateway_utils.shutil, "move", side_effect=RuntimeError("boom")
            ):
                saved = gateway_utils.save_service(
                    metadata=metadata,
                    service_dir=os.path.join(tmp, "incoming"),
                    service_hash=service_hash,
                )

            self.assertFalse(saved)
            self.assertFalse(os.path.exists(os.path.join(metadata_registry, service_hash)))


if __name__ == "__main__":
    unittest.main()

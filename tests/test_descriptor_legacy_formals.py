"""The descriptor files of the formals that the code builds hold the bytes of the code (#693).

These formals are on the wire today, so the files copy them and have no class= and no
name= line (contract/descriptors/REASON.md, rule 6.4). Each test calls the code and
compares the bytes with the formal of the file.
"""
import os
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.identity import transport_stack
    from src.utils import descriptor_file
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

ROOT = os.path.join(os.path.dirname(__file__), "..", "contract", "descriptors")


def _formal(rel):
    return descriptor_file.read(os.path.join(ROOT, rel)).formal


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class LayerFormalTests(unittest.TestCase):
    def test_the_layer_files_hold_the_formals_of_the_code(self):
        for name, build in (
            ("tcp", lambda: transport_stack.transport_component("tcp")),
            ("tor-onion-v3", transport_stack.onion_transport_component),
            ("tls", transport_stack.tls_component),
            ("http2", transport_stack.http2_component),
            ("grpc", transport_stack.grpc_component),
        ):
            with self.subTest(name):
                tags, _, formal = build()
                self.assertEqual(list(tags), [name])
                self.assertEqual(_formal(f"protocols/address/{name}.descriptor"), formal)


if __name__ == "__main__":
    unittest.main()

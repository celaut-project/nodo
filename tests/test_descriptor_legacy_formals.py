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
    from src.identity.node_identity import SIGNATURE_SCHEME_COMPONENTS
    from src.utils import descriptor_file, ledger_descriptors
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

ROOT = os.path.join(os.path.dirname(__file__), "..", "contract", "descriptors")


def _formal(rel):
    return descriptor_file.read(os.path.join(ROOT, rel)).formal


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class LayerFormalTests(unittest.TestCase):
    """The formal of bee-rpc names the hash type of the configuration: the files are of
    the example configuration."""

    def test_the_layer_files_hold_the_formals_of_the_code(self):
        for name, build in (
            ("tcp", lambda: transport_stack.transport_component("tcp")),
            ("tor-onion-v3", transport_stack.onion_transport_component),
            ("tls", transport_stack.tls_component),
            ("http2", transport_stack.http2_component),
            ("grpc", transport_stack.grpc_component),
            ("bee-rpc", transport_stack.bee_rpc_component),
            ("celaut-gateway", transport_stack.gateway_component),
        ):
            with self.subTest(name):
                tags, _, formal = build()
                self.assertEqual(list(tags), [name])
                self.assertEqual(_formal(f"protocols/address/{name}.descriptor"), formal)


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class LedgerFormalTests(unittest.TestCase):
    """The formal of a ledger depends on the configuration: the files are of mainnet."""

    def test_the_ledger_files_hold_the_mainnet_formals_of_the_code(self):
        for name, build in (
            ("ergo-mainnet", ledger_descriptors.ergo_payment_ledger),
            ("bitcoin-mainnet", ledger_descriptors.bitcoin_payment_ledger),
        ):
            with self.subTest(name):
                ledger = build()
                self.assertEqual(list(ledger.tags), [name], "the configuration is not mainnet")
                self.assertEqual(_formal(f"ledgers/{name}.descriptor"), ledger.formal)


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class SignatureSchemeFormalTests(unittest.TestCase):
    def test_the_ed25519_file_holds_the_formal_of_the_code(self):
        (component,) = SIGNATURE_SCHEME_COMPONENTS
        self.assertEqual(list(component.tags), ["ed25519"])
        self.assertEqual(_formal("signatures/schemes/ed25519.descriptor"), component.formal)


if __name__ == "__main__":
    unittest.main()

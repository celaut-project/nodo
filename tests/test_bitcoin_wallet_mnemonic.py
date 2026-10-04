"""Which Bitcoin backends get a mnemonic minted for them on load.

Every backend that signs does -- `explorer` locally, `service` through its bitcoind --
and `core` does not, because its keys are the operator's own bitcoind's and a phrase
nothing uses would be written into config.yaml and labelled a secret to back up.
"""
import tempfile
import unittest
from pathlib import Path

from src.utils.config import ConfigManager
from src.utils.config_validation import ConfigValidationError
from src.utils.singleton import Singleton

BODY = """network:
  GATEWAY_PORT: 4040
ledgers:
  bitcoin:
    BACKEND: {backend}
    WALLET_MNEMONIC: "{mnemonic}"
{extra}{top}"""

# What `service` needs before it is accepted at all: an id to run, and the credentials.
SERVICE = (
    "    RPC_USER: nodo\n    RPC_PASSWORD: hunter2\n",
    "core_services:\n  bitcoin-node: " + "a1" * 32 + "\n",
)


def _load(backend, mnemonic="", extra="", top=""):
    path = Path(tempfile.mkdtemp()) / "config.yaml"
    path.write_text(BODY.format(backend=backend, mnemonic=mnemonic, extra=extra, top=top))
    Singleton._instances.pop(ConfigManager, None)
    manager = ConfigManager(config_path=str(path))
    manager.load_config(force_reload=True)
    return manager


class BitcoinMnemonicTests(unittest.TestCase):
    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def test_explorer_and_service_are_given_a_wallet(self):
        for backend, (extra, top) in (("explorer", ("", "")), ("service", SERVICE)):
            with self.subTest(backend=backend):
                generated = _load(backend, extra=extra, top=top).get(
                    "ledgers.bitcoin.WALLET_MNEMONIC"
                )
                self.assertEqual(len(generated.split()), 12)

    def test_core_is_left_without_one(self):
        self.assertFalse(_load("core").get("ledgers.bitcoin.WALLET_MNEMONIC"))

    def test_a_mnemonic_the_operator_pasted_is_kept(self):
        words = "abandon " * 11 + "about"
        self.assertEqual(
            _load("explorer", mnemonic=words).get("ledgers.bitcoin.WALLET_MNEMONIC"), words
        )

    def test_the_generated_mnemonic_is_a_wallet_the_explorer_can_use(self):
        from src.payment_system.contracts.bitcoin import signer

        generated = _load("explorer").get("ledgers.bitcoin.WALLET_MNEMONIC")
        self.assertTrue(signer.derive_wallet_key(generated).address.startswith("bc1q"))

    def test_the_retired_external_keys_flag_is_refused_rather_than_ignored(self):
        """`true` meant "no key in this file", and ignoring it would mint one anyway."""
        with self.assertRaisesRegex(ConfigValidationError, "WALLET_KEYS_EXTERNAL"):
            _load("explorer", extra="    WALLET_KEYS_EXTERNAL: true\n")


if __name__ == "__main__":
    unittest.main()

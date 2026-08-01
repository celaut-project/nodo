"""Nested single-wallet config: nesting, rejection of removed keys, validation (#186 phase 1)."""
import tempfile
import unittest
from pathlib import Path

from src.utils.config import ConfigManager
from src.utils.config_validation import (
    ConfigValidationError,
    validate_ergo_config,
)
from src.utils.singleton import Singleton

VALID_MNEMONIC = "abandon " * 11 + "about"  # shape only; never a real funded seed
COLD = "9gGZp7HRAFxgGWSwvS4hCbxM2RpkYr6pHvwpU4GPrpvxY7Y2nQo"

BASE = f"""network:
  GATEWAY_PORT: 4040
ledgers:
  ergo:
    NODE_URL: "https://node.example"
    WALLET_MNEMONIC: "{VALID_MNEMONIC}"
    reputation:
      LEDGER_REPUTATION_SUBMISSION_THRESHOLD: 10
      TOTAL_REPUTATION_TOKEN_AMOUNT: 1000000000
      REPUTATION_PROOF_ID: ""
    payments:
      HOT_WALLET_LIMITS: "100"
      COLD_WALLET: ""
      COLD_WALLET_MIN_TRANSFER: "1"
"""


def _fresh(path: str) -> ConfigManager:
    Singleton._instances.pop(ConfigManager, None)
    mgr = ConfigManager(config_path=path)
    mgr.load_config(force_reload=True)
    return mgr


class SingleWalletConfigTests(unittest.TestCase):
    def tearDown(self):
        Singleton._instances.pop(ConfigManager, None)

    def _write(self, body: str) -> str:
        d = tempfile.mkdtemp()
        p = Path(d) / "config.yaml"
        p.write_text(body)
        return str(p)

    def test_nested_paths_resolve(self):
        mgr = _fresh(self._write(BASE))
        self.assertEqual(mgr.get("ledgers.ergo.payments.HOT_WALLET_LIMITS"), "100")
        self.assertEqual(mgr.get("ledgers.ergo.reputation.TOTAL_REPUTATION_TOKEN_AMOUNT"), 1000000000)
        self.assertEqual(mgr.get("ledgers.ergo.payments.COLD_WALLET_MIN_TRANSFER"), "1")

    def test_removed_keys_rejected_on_load(self):
        for removed in ("AUXILIARY_MNEMONIC", "AUXILIAR_MNEMONIC",
                        "PAYMENTS_RECEIVER_WALLET", "PAYMENTS_RECIVER_WALLET"):
            body = BASE.replace(
                '    payments:\n',
                f'    payments:\n      {removed}: ""\n',
            )
            with self.assertRaises(ConfigValidationError, msg=f"{removed} must be rejected"):
                _fresh(self._write(body))

    def test_valid_config_passes_validation(self):
        mgr = _fresh(self._write(BASE))
        # No cold wallet, features on -> mnemonic present so it validates.
        mgr.validate_ergo(payments_enabled=True, reputation_enabled=True)

    def test_missing_mnemonic_rejected_when_enabled(self):
        body = BASE.replace(f'WALLET_MNEMONIC: "{VALID_MNEMONIC}"', 'WALLET_MNEMONIC: ""')
        mgr = _fresh(self._write(body))
        with self.assertRaises(ConfigValidationError):
            mgr.validate_ergo(payments_enabled=True, reputation_enabled=True)
        # ...but fine when both features are disabled.
        mgr.validate_ergo(payments_enabled=False, reputation_enabled=False)

    def test_invalid_cold_wallet_rejected(self):
        body = BASE.replace('COLD_WALLET: ""', 'COLD_WALLET: "not-a-valid-address"')
        mgr = _fresh(self._write(body))
        with self.assertRaises(ConfigValidationError):
            mgr.validate_ergo(payments_enabled=True, reputation_enabled=False)

    def test_valid_cold_wallet_accepted(self):
        body = BASE.replace('COLD_WALLET: ""', f'COLD_WALLET: "{COLD}"')
        mgr = _fresh(self._write(body))
        mgr.validate_ergo(payments_enabled=True, reputation_enabled=False)

    def test_no_migration_test_present(self):
        # Guard: removed keys must be flagged, never silently migrated.
        cfg = {"ledgers": {"ergo": {"WALLET_MNEMONIC": "m", "AUXILIAR_MNEMONIC": ""}}}
        with self.assertRaises(ConfigValidationError):
            validate_ergo_config(cfg, payments_enabled=False, reputation_enabled=False)


if __name__ == "__main__":
    unittest.main()

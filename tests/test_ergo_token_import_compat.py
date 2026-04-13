import unittest
from pathlib import Path


class ErgoTokenImportCompatTests(unittest.TestCase):
    def test_transaction_has_sdk_fallback_for_ergo_token(self):
        content = Path("src/reputation_system/contracts/ergo/transaction.py").read_text(encoding="utf-8")
        self.assertIn("jpype.JPackage(\"org\").ergoplatform.appkit", content)
        self.assertIn("org_appkit.ErgoToken", content)
        self.assertIn("jpype.JPackage(\"org\").ergoplatform.sdk.ErgoToken", content)


if __name__ == "__main__":
    unittest.main()

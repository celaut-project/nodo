import builtins
import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

if "mnemonic" not in sys.modules:
    mnemonic_module = types.ModuleType("mnemonic")

    class _Mnemonic:
        def __init__(self, *args, **kwargs):
            pass

        def generate(self, strength=128):
            return "test mnemonic"

    mnemonic_module.Mnemonic = _Mnemonic
    sys.modules["mnemonic"] = mnemonic_module

from src.utils.java_dependency import JavaDependencyMissing, build_java_dependency_message


BLOCKED_PREFIXES = ("ergpy", "jpype", "java", "org.ergoplatform")


def _purge_modules(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(f"{prefix}."):
            sys.modules.pop(name, None)


class JavaDependencyLazyImportTests(unittest.TestCase):
    def test_java_runtime_requires_configured_java_home_instead_of_system_java(self):
        from src.utils.java_dependency import ensure_java_runtime

        with mock.patch.dict("os.environ", {"JAVA_HOME": "/definitely/missing/java-home"}, clear=False):
            with self.assertRaises(JavaDependencyMissing):
                ensure_java_runtime(feature="pagos Ergo o reputacion")

    def test_startup_modules_do_not_have_eager_java_imports(self):
        targets = {
            "src/gateway/gateway.py": [
                "from src.reputation_system.contracts.ergo.proof_validation import sign_message",
            ],
            "src/manager/manager.py": [
                "from src.reputation_system.contracts.ergo.proof_validation import validate_contract_ledger",
            ],
            "src/payment_system/payment_process.py": [
                "from src.payment_system.contracts.ergo import interface as ergo",
                "from src.reputation_system.interface import update_vmachine_reputation, update_peer_reputation",
            ],
            "src/reputation_system/interface.py": [
                "from src.reputation_system.contracts.ergo.transaction import submit_reputation_proof",
            ],
        }

        for path, forbidden_imports in targets.items():
            content = Path(path).read_text(encoding="utf-8")
            preamble = content.split("\nclass ", 1)[0].split("\ndef ", 1)[0]
            for forbidden_import in forbidden_imports:
                self.assertNotIn(forbidden_import, preamble)

    def test_increase_peer_deposit_prints_controlled_message_when_java_is_missing(self):
        from src.commands.increase_peer_deposit import increase_peer_deposit

        fake_module = types.ModuleType("src.payment_system.payment_process")

        def _raise(*args, **kwargs):
            raise JavaDependencyMissing(build_java_dependency_message(feature="Ergo payments or reputation"))

        fake_module.increase_deposit_on_peer = _raise
        stdout = io.StringIO()

        with mock.patch.dict(sys.modules, {"src.payment_system.payment_process": fake_module}):
            with redirect_stdout(stdout):
                increase_peer_deposit("peer-1", 5)

        self.assertIn("Java no esta instalado o disponible", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

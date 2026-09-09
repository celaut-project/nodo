"""The accessors the donation circuit reads off `contracts.envs`, and that they exist.

This file exists because of a regression it would have caught. Rewriting `envs.py` over
the contract registry (#340) dropped `donation_scanners()` and `seconds_per_block()`
without touching any of their four callers, and nothing failed: `nodo donations`, the
balancer's credit lookup and the hourly indexer all reach them through a `try/except`
or have them mocked in their own tests, so the circuit went quiet instead of breaking.
A node would have indexed no donations, counted no credit, and routed as though nobody
had ever donated -- which is indistinguishable, from the outside, from a network where
nobody does.

Two lessons, and both are tests here rather than notes:

* mocking an attribute in every test of every caller means no test asserts it exists;
* comparing a branch's failures against its own parent's tip cannot catch a regression
  the branch itself introduces -- which is how this survived a full-suite check.
"""
import re
import unittest
import unittest.mock
from pathlib import Path

IMPORT_ERROR = None
try:
    from src.payment_system.contracts import envs
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    envs = None  # type: ignore[assignment]

#: Everything that reads the payment-envs dispatch on behalf of donations.
READERS = (
    "src/payment_system/donations/credit.py",
    "src/payment_system/donations/indexer.py",
    "src/payment_system/donations/accrual.py",
    "src/payment_system/donations/payout.py",
    "src/commands/donations.py",
)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EnvsSurfaceTests(unittest.TestCase):
    """Every `envs.<name>` the donation code names is really there."""

    def test_every_accessor_the_donation_code_reads_exists(self):
        missing = []
        for path in READERS:
            source = Path(path).read_text(encoding="utf-8")
            for name in sorted(set(re.findall(r"\benvs\.([a-zA-Z_][a-zA-Z0-9_]*)", source))):
                if not hasattr(envs, name):
                    missing.append(f"{path} reads envs.{name}, which does not exist")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_the_readers_are_the_ones_that_actually_read(self):
        # If a new module starts reading `envs`, it belongs in READERS above -- so the
        # list is checked against the tree rather than trusted.
        unlisted = []
        for path in Path("src/payment_system/donations").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "envs." in source and str(path) not in READERS:
                unlisted.append(str(path))
        self.assertEqual(unlisted, [], "these read envs and are not covered above")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DonationScannerTests(unittest.TestCase):
    def test_scanners_are_keyed_by_ledger_tag(self):
        """Per ledger, not per contract or per method, and deliberately so.

        A counted donation wallet is a property of the chain: one address receives
        whatever is sent to it, through any contract and in any asset, so there is
        exactly one way to read what reached it.
        """
        scanners = envs.donation_scanners()
        self.assertIsInstance(scanners, dict)
        for tag, scanner in scanners.items():
            self.assertIsInstance(tag, str)
            self.assertEqual(getattr(scanner, "LEDGER", None), tag)
            # What the indexer actually calls on it. A module missing one of these is
            # not a scanner, and would fail on the hourly tick rather than here.
            for call in ("chain_height", "scan_address", "instance_values_for",
                         "native_to_mu"):
                self.assertTrue(callable(getattr(scanner, call, None)),
                                f"{tag}'s scanner cannot {call}")

    def test_ergo_contributes_a_scanner_when_its_light_module_imports(self):
        # The scanning module is light on purpose -- indexing is a read, and must not
        # need a wallet, a signature or a JVM -- so it is present in this environment.
        self.assertIn("ergo", envs.donation_scanners())

    def test_a_scanner_that_will_not_import_takes_nothing_else_down(self):
        # One try per module: a node whose second ledger's scanner is broken still
        # counts donations on the first.
        with unittest.mock.patch.object(
            envs, "DONATION_SCAN_MODULES",
            ("src.payment_system.contracts.ergo.donation_scan", "nodo.no.such.module"),
        ), unittest.mock.patch.object(envs, "LOGGER", lambda _m: None):
            self.assertEqual(list(envs.donation_scanners()), ["ergo"])

    def test_block_time_is_answered_in_seconds_and_zero_when_unknown(self):
        # Donation age is measured in seconds because an Ergo block is ~120 s and a
        # Bitcoin block ~600 s: in blocks, the same old donation would weigh five times
        # differently depending on the chain it was paid on.
        self.assertGreater(envs.seconds_per_block("ergo"), 0)
        self.assertEqual(envs.seconds_per_block("no-such-ledger"), 0)


if __name__ == "__main__":
    unittest.main()

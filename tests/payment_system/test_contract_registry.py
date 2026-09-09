"""Every contract the node offers has the shape the payment flow dispatches against.

`envs.py` used to write out the same pair by hand, six times, with `ergo` named
literally in each dict. The registry is what made "nodo allows the simultaneous use of
multiple ledgers" true rather than aspirational -- and the property that has to hold for
that is dull: a contract either has the whole shape and is offered, or it is not offered
at all. A module missing one member would fail on the payment that needed it, which is
the worst possible moment.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts import envs, registry
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    registry = None  # type: ignore[assignment]


def _contract(ledger="fake", contract="fake-type", **extra):
    module = mock.Mock()
    module.CONTRACT = contract
    module.CONTRACT_HASH = f"hash-of-{contract}"
    module.LEDGER = ledger
    module.is_demo = False
    module.needs_unspent_proof = False
    module.DEPOSIT_TOKEN_TTL = 3600
    module.unavailable_reason = mock.Mock(return_value=None)
    for key, value in extra.items():
        setattr(module, key, value)
    return module


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ProtocolTests(unittest.TestCase):

    def test_every_declared_candidate_satisfies_the_protocol(self):
        """Checked against the modules themselves, not against a stub.

        The simulated contract used to be missing `LEDGER`, `ledger()`, `init()` and
        `manager()` outright -- which is why `init_interfaces()` and
        `manage_interfaces()` could only ever have held Ergo.
        """
        from importlib import import_module

        for candidate in registry.CANDIDATES:
            with self.subTest(candidate=candidate.name):
                try:
                    module = import_module(candidate.module_path)
                except Exception as exc:  # pragma: no cover - environment-dependent
                    self.skipTest(f"{candidate.name} will not import here: {exc}")
                missing = [m for m in registry.REQUIRED_MEMBERS if not hasattr(module, m)]
                self.assertEqual(missing, [], f"{candidate.name} is missing {missing}")

    def test_every_candidate_with_a_rate_module_contributes_a_display_unit(self):
        from importlib import import_module

        for path in registry.rate_modules():
            with self.subTest(rate=path):
                try:
                    module = import_module(path)
                except Exception as exc:  # pragma: no cover - environment-dependent
                    self.skipTest(f"{path} will not import here: {exc}")
                self.assertTrue(callable(module.display_units))

    def test_a_module_missing_a_member_is_not_offered(self):
        broken = _contract()
        del broken.process_payment
        with mock.patch.object(registry, "CANDIDATES", (registry._Candidate("broken", "x"),)), \
                mock.patch.object(registry, "_configured", return_value=True), \
                mock.patch("src.payment_system.contracts.registry.import_module",
                           return_value=broken):
            registry.forget_reports()
            self.assertEqual(registry.contracts(), {})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AvailabilityTests(unittest.TestCase):

    def _offered(self, module, configured=True):
        with mock.patch.object(registry, "CANDIDATES", (registry._Candidate("x", "x.y"),)), \
                mock.patch.object(registry, "_configured", return_value=configured), \
                mock.patch("src.payment_system.contracts.registry.import_module",
                           return_value=module):
            registry.forget_reports()
            return registry.contracts()

    def test_a_ledger_nobody_configured_is_simply_not_offered(self):
        self.assertEqual(self._offered(_contract(), configured=False), {})

    def test_a_contract_that_cannot_settle_is_not_offered(self):
        """Degrades to "not offered", never to an exception.

        A contract that cannot settle must not be advertised: a peer that reads it out
        of `GetPeerInfo` and pays through it has paid into nothing. And the check has to
        be cheap -- the registry is asked on the payment path.
        """
        unusable = _contract()
        unusable.unavailable_reason = mock.Mock(return_value="no Java")
        self.assertEqual(self._offered(unusable), {})

    def test_a_module_that_will_not_import_is_not_offered_and_does_not_raise(self):
        with mock.patch.object(registry, "CANDIDATES", (registry._Candidate("x", "x.y"),)), \
                mock.patch.object(registry, "_configured", return_value=True), \
                mock.patch("src.payment_system.contracts.registry.import_module",
                           side_effect=OSError("no libjvm")):
            registry.forget_reports()
            self.assertEqual(registry.contracts(), {})

    def test_a_usable_contract_is_keyed_by_its_contract_hash(self):
        module = _contract()
        self.assertEqual(self._offered(module), {module.CONTRACT_HASH: module})

    def test_it_says_why_once_rather_than_per_payment(self):
        # Reached from the payment path and from every advertisement, so a per-call log
        # line would be a log line per payment.
        unusable = _contract()
        unusable.unavailable_reason = mock.Mock(return_value="no Java")
        said = []
        # All three calls under the same patches, so the only candidate in play is the
        # unusable one -- otherwise the real registry reports its own ledgers too.
        with mock.patch.object(registry, "CANDIDATES", (registry._Candidate("x", "x.y"),)), \
                mock.patch.object(registry, "_configured", return_value=True), \
                mock.patch("src.payment_system.contracts.registry.import_module",
                           return_value=unusable), \
                mock.patch.object(registry, "LOGGER", said.append):
            registry.forget_reports()
            registry.contracts()
            registry.contracts()
            registry.contracts()
        self.assertEqual(len(said), 1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DispatchTests(unittest.TestCase):
    """`envs` is one comprehension per question, over whatever is offered."""

    def _envs(self, *modules):
        return mock.patch.object(
            envs, "contracts", return_value={m.CONTRACT_HASH: m for m in modules}
        )

    def test_every_dict_covers_every_offered_contract(self):
        a, b = _contract(contract="a"), _contract(contract="b")
        with self._envs(a, b):
            for name in ("payment_process_validators", "available_payment_process",
                         "check_sender_balances", "settlement_floors",
                         "init_interfaces", "manage_interfaces"):
                with self.subTest(dispatch=name):
                    self.assertEqual(
                        set(getattr(envs, name)()), {a.CONTRACT_HASH, b.CONTRACT_HASH}
                    )

    def test_demos_are_a_payer_side_notion_and_are_live(self):
        real, demo = _contract(contract="real"), _contract(contract="demo", is_demo=True)
        with self._envs(real, demo):
            self.assertEqual(envs.DEMOS, (demo.CONTRACT_HASH,))
        with self._envs(real):
            # A live value, not a snapshot taken at import: it used to be computed the
            # first time anything imported the module.
            self.assertEqual(envs.DEMOS, ())

    def test_only_contracts_that_need_an_unspent_output_are_named(self):
        needs, does_not = _contract(contract="ergo-like", needs_unspent_proof=True), _contract()
        with self._envs(needs, does_not):
            self.assertEqual(envs.needs_unspent_proof(), (needs.CONTRACT_HASH,))

    def test_each_contract_carries_its_own_deposit_ttl(self):
        fast = _contract(contract="fast", DEPOSIT_TOKEN_TTL=3600)
        slow = _contract(contract="slow", DEPOSIT_TOKEN_TTL=21600)
        with self._envs(fast, slow):
            self.assertEqual(
                envs.deposit_token_ttls(),
                {fast.CONTRACT_HASH: 3600, slow.CONTRACT_HASH: 21600},
            )

    def test_two_contracts_declaring_one_display_unit_is_a_configuration_error(self):
        # `dict.update` would let the second silently win, so an operator's chosen
        # display unit would mean whichever rate module imported last.
        clashing = mock.Mock()
        clashing.display_units.return_value = {"btc": {"SYMBOL": "BTC"}}
        # Patched on `envs` rather than on `importlib`: the module imports the name
        # once, at import time, so patching the package would leave the already-bound
        # reference alone and this test would pass whatever the code did.
        with mock.patch.object(registry, "rate_modules", return_value=["a", "b"]), \
                mock.patch("src.payment_system.contracts.envs.rate_modules",
                           return_value=["a", "b"]), \
                mock.patch.object(envs, "import_module", return_value=clashing):
            with self.assertRaisesRegex(ValueError, "display unit"):
                envs.display_units()


if __name__ == "__main__":
    unittest.main()

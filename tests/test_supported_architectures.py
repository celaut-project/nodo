"""Which architectures the node advertises is DERIVED, never declared.

The pair of config flags this replaces (`builder.ARM_SUPPORT` / `X86_SUPPORT`)
could disagree with reality in both directions. Set to true on a host that could
not run that arch, a service was accepted and then died deep inside the Cloud
Hypervisor build looking for a guest kernel nothing had installed; set to false on
a host that could, the node hid capacity it had.

``resolve_supported_architectures`` is pure for exactly this reason: what a node
claims it can execute is checkable here, with no install and no config.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.utils.architectures import resolve_supported_architectures
    from src.utils import config_validation
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    resolve_supported_architectures = None  # type: ignore[assignment]
    config_validation = None  # type: ignore[assignment]

ARM = "linux/arm64"
AMD = "linux/amd64"


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DerivedSupportedArchitecturesTests(unittest.TestCase):
    def _canonical(self, host_arch, emulated=(), probe=None):
        entries = resolve_supported_architectures(
            host_arch, probe or (lambda arch: arch in emulated)
        )
        return [aliases[0] for aliases in entries]

    def test_host_arch_is_always_executable(self):
        # It runs under CH/KVM: no config, no emulator, nothing optional in the way.
        self.assertEqual(self._canonical(AMD), [AMD])
        self.assertEqual(self._canonical(ARM), [ARM])

    def test_emulatable_foreign_arch_is_added(self):
        self.assertEqual(self._canonical(AMD, emulated=(ARM,)), [AMD, ARM])
        self.assertEqual(self._canonical(ARM, emulated=(AMD,)), [ARM, AMD])

    def test_host_arch_comes_first(self):
        # Native capacity must not read as an afterthought of the emulated list.
        self.assertEqual(self._canonical(ARM, emulated=(AMD,))[0], ARM)

    def test_foreign_arch_without_emulation_is_not_advertised(self):
        # The failure this whole change is about: advertising arm64 on an x86_64
        # host that has no arm64 guest kernel.
        self.assertEqual(self._canonical(AMD), [AMD])

    def test_the_host_arch_is_never_probed_for_emulation(self):
        probed = []

        def probe(arch):
            probed.append(arch)
            return True

        self.assertEqual(self._canonical(AMD, probe=probe), [AMD, ARM])
        self.assertEqual(probed, [ARM])

    def test_unknown_host_arch_advertises_nothing_native(self):
        # A machine type with no alias table (riscv64, say) yields no native entry
        # rather than a wrong one.
        self.assertEqual(self._canonical(None), [])
        self.assertEqual(self._canonical("linux/riscv64"), [])

    def test_emulation_probe_failure_costs_only_the_foreign_arch(self):
        def probe(_arch):
            raise RuntimeError("qemu config is broken")

        self.assertEqual(self._canonical(AMD, probe=probe), [AMD])

    def test_entries_carry_the_short_aliases_with_the_canonical_tag(self):
        # Callers match a service's raw architecture tag against these lists, so
        # "aarch64" and "x86_64" have to travel with the canonical form.
        entries = resolve_supported_architectures(AMD, lambda _arch: True)
        by_canonical = {entry[0]: entry for entry in entries}
        self.assertIn("x86_64", by_canonical[AMD])
        self.assertIn("aarch64", by_canonical[ARM])

    def test_result_is_not_shared_state(self):
        # The table is a module global that other modules index into; handing out
        # the same inner lists twice would let one caller's edit reach another.
        first = resolve_supported_architectures(AMD, lambda _arch: True)
        second = resolve_supported_architectures(AMD, lambda _arch: True)
        first[0].append("mutated")
        self.assertNotIn("mutated", second[0])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RemovedArchitectureFlagsTests(unittest.TestCase):
    def test_the_old_declaration_flags_are_rejected(self):
        # Left in place they would be silently ignored, and an operator would keep
        # believing a false statement about what the node executes.
        for key in ("ARM_SUPPORT", "X86_SUPPORT"):
            with self.subTest(key=key):
                self.assertIn(key, config_validation.REMOVED_KEYS)
                self.assertEqual(
                    config_validation._find_removed_keys({"builder": {key: True}}),
                    [f"builder.{key}"],
                )

    def test_the_packer_flags_are_still_allowed(self):
        # Cross-arch PACKING really is impossible here, so those stay configurable.
        self.assertEqual(
            config_validation._find_removed_keys(
                {"packer": {"ARM_PACKER_SUPPORT": True, "X86_PACKER_SUPPORT": True}}
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()

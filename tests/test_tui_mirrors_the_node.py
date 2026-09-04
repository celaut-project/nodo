"""The TUI's copies of the node's constants have to agree with the node's.

The pricing page does not ask the node anything. It reads and writes `config.yaml`
itself, so it has to know the same config keys, the same architecture tags and the
same guest-kernel reserve the Python side reads -- and it is a separate Rust binary,
so it cannot import any of them. Those constants are duplicated by necessity.

`src/commands/tui/build.rs` states the rule this file enforces, for the proto schema
it generates rather than duplicates: *"A second copy of a wire contract has no way to
stay honest; there is now only one."* A config key or an arch tag is the same kind of
contract. Where a second copy genuinely cannot be removed,
the next best thing is that it cannot drift in silence -- and the failure mode is
quiet in both directions. The TUI writes `pricing.BY_ARCH.<arch>.<key>` and the node
reads it back: a key that disagrees means the operator sets a price on a page that
says it took effect, and no guest is ever charged it. A reserve that disagrees means
the page advises against an overhead the node does not apply, which is precisely what
the operator is looking at that number to avoid.

So these are pinned here rather than by a comment asking the next person to remember.
Parsing Rust source from a Python test is not elegant; it is the only place both
sides are visible at once, and it costs one regex per constant.
"""
import re
import unittest
from pathlib import Path

IMPORT_ERROR = None
try:
    from src.utils.arch_guard import CANONICAL_ARCHITECTURES
    from src.utils.config_validation import PER_ARCH_PRICE_KEYS
    from src.utils.monetary import PRICING_BY_ARCH_KEY
    from src.virtualizers.microvm import limits
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    CANONICAL_ARCHITECTURES = ()  # type: ignore[assignment]
    PER_ARCH_PRICE_KEYS = ()  # type: ignore[assignment]
    PRICING_BY_ARCH_KEY = None  # type: ignore[assignment]
    limits = None  # type: ignore[assignment]

APP_RS = Path(__file__).resolve().parent.parent / "src" / "commands" / "tui" / "src" / "app.rs"


def _rust_source():
    return APP_RS.read_text(encoding="utf-8")


def _rust_str_const(source, name):
    """The value of `const <name>: &str = "...";`."""
    match = re.search(rf'const\s+{name}\s*:\s*&str\s*=\s*"([^"]*)"\s*;', source)
    assert match, f"{name} not found in app.rs"
    return match.group(1)


def _rust_f64_const(source, name):
    match = re.search(rf'const\s+{name}\s*:\s*f64\s*=\s*([0-9._]+)\s*;', source)
    assert match, f"{name} not found in app.rs"
    return float(match.group(1).replace("_", ""))


def _rust_str_array(source, name):
    """The elements of `const <name>: [&str; N] = ["a", "b"];`."""
    match = re.search(rf'const\s+{name}\s*:\s*\[&str;\s*\d+\]\s*=\s*\[(.*?)\]\s*;', source, re.S)
    assert match, f"{name} not found in app.rs"
    return tuple(re.findall(r'"([^"]*)"', match.group(1)))


def _rust_reserve_table(source):
    """`DEFAULT_GUEST_KERNEL_RESERVE`, as {arch: fixed_mib}.

    The ratio is a named constant in both languages and is checked on its own; only
    the fixed part is written per arch on either side.
    """
    match = re.search(
        r'const\s+DEFAULT_GUEST_KERNEL_RESERVE\s*:\s*\[\(&str,\s*u64,\s*f64\);\s*\d+\]\s*=\s*\[(.*?)\]\s*;',
        source,
        re.S,
    )
    assert match, "DEFAULT_GUEST_KERNEL_RESERVE not found in app.rs"
    return {
        arch: int(mib)
        for arch, mib in re.findall(r'\(\s*"([^"]+)"\s*,\s*(\d+)\s*,', match.group(1))
    }


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheTuiWritesTheKeysTheNodeReadsTests(unittest.TestCase):
    """Config keys. A disagreement here silently discards an operator's edit."""

    def setUp(self):
        self.source = _rust_source()

    def test_the_per_arch_pricing_block_has_the_same_name(self):
        self.assertEqual(
            _rust_str_const(self.source, "PRICING_BY_ARCH_KEY"), PRICING_BY_ARCH_KEY
        )

    def test_the_same_prices_may_be_set_per_arch(self):
        # The TUI offers a per-arch row for exactly these. Offering one the validator
        # rejects means the node refuses to start on a config its own editor wrote.
        self.assertEqual(
            _rust_str_array(self.source, "PER_ARCH_PRICE_KEYS"),
            tuple(PER_ARCH_PRICE_KEYS),
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheTuiKnowsTheSameArchitecturesTests(unittest.TestCase):
    """Arch tags, against the one list that defines them."""

    def setUp(self):
        self.source = _rust_source()

    def test_the_priced_architectures_are_the_canonical_ones(self):
        # Order is not the contract -- these drive a lookup, not a sequence -- but
        # membership is: an arch the TUI can price and the validator rejects is a
        # config the node will not load, and an arch the node prices and the TUI
        # cannot see is a policy the operator has no way to edit.
        self.assertEqual(
            set(_rust_str_array(self.source, "PRICED_ARCHITECTURES")),
            set(CANONICAL_ARCHITECTURES),
        )

    def test_every_canonical_architecture_has_a_measured_reserve(self):
        # `_reserve_for_arch` falls back to the largest known reserve for an arch it
        # has no entry for. That is the right answer for an arch nobody characterised
        # and the wrong one for an arch the node knows how to boot, so adding a tag
        # to `arch_guard.ARCH_ALIASES` has to come with a measurement.
        self.assertEqual(
            set(limits._DEFAULT_GUEST_KERNEL_RESERVE), set(CANONICAL_ARCHITECTURES)
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheTuiAdvisesTheReserveTheNodeAppliesTests(unittest.TestCase):
    """The reserve. A disagreement here misprices memory, quietly."""

    def setUp(self):
        self.source = _rust_source()

    def test_the_ratio_matches(self):
        self.assertAlmostEqual(
            _rust_f64_const(self.source, "GUEST_KERNEL_RESERVE_RATIO"),
            limits._GUEST_KERNEL_RESERVE_RATIO,
            places=9,
        )

    def test_the_fixed_part_matches_per_arch(self):
        rust = _rust_reserve_table(self.source)
        python = {
            arch: fixed_mib
            for arch, (fixed_mib, _) in limits._DEFAULT_GUEST_KERNEL_RESERVE.items()
        }
        self.assertEqual(rust, python)

    def test_the_ratio_is_the_one_both_arches_use(self):
        # The Python table builds both entries from the shared constant. If an arch
        # ever needs its own default ratio, this test is the reminder that the Rust
        # mirror has to grow a per-arch ratio too rather than keep one constant.
        ratios = {ratio for _, ratio in limits._DEFAULT_GUEST_KERNEL_RESERVE.values()}
        self.assertEqual(ratios, {limits._GUEST_KERNEL_RESERVE_RATIO})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TheReserveRatioStaysNearThePhysicsTests(unittest.TestCase):
    """Why 0.025 and not 0.05, pinned so the reasoning cannot be lost.

    The reserve is never billed -- the node absorbs it -- so margin on the ratio is
    host RAM the operator commits and cannot charge for, and it scales with every
    guest. Margin on the fixed part does not scale, which is why that is where it
    belongs.
    """

    # One `struct page` (64 B) per 4 KiB frame: the share of any guest the kernel
    # cannot avoid spending on describing it.
    STRUCT_PAGE_FLOOR = 64 / 4096

    # Fitted against `usable` from the per-`-m` measurements in limits.py:
    # overhead = fixed + ratio * usable.
    MEASURED = {
        "linux/amd64": (31.4, 0.0180),
        "linux/arm64": (23.1, 0.0210),
    }
    MEASURED_RATIO = {arch: ratio for arch, (_, ratio) in MEASURED.items()}

    def test_the_ratio_covers_the_struct_page_floor(self):
        self.assertGreater(limits._GUEST_KERNEL_RESERVE_RATIO, self.STRUCT_PAGE_FLOOR)

    def test_the_ratio_covers_every_measurement(self):
        for arch, measured in self.MEASURED_RATIO.items():
            with self.subTest(arch=arch):
                self.assertGreater(limits._GUEST_KERNEL_RESERVE_RATIO, measured)

    def test_the_ratio_does_not_double_the_worst_measurement(self):
        # The guard against the figure this replaced. A flat 0.05 was 2.4x the
        # measured amd64 ratio, which on an 8 GiB guest reserved 450 MiB where the
        # kernel takes ~180. Headroom, not a second kernel's worth of it.
        worst = max(self.MEASURED_RATIO.values())
        self.assertLess(limits._GUEST_KERNEL_RESERVE_RATIO, 2 * worst)

    def test_a_large_guest_is_not_over_reserved_by_more_than_half_again(self):
        # The property that actually matters, stated at the size where a bad ratio
        # hurts: what the node reserves for an 8 GiB guest against what its kernel
        # was measured to take. Above the measurement (or the guest OOMs below its
        # declared ceiling) and within half again of it (or the operator commits
        # host RAM they cannot bill). A flat 0.05 ratio failed the upper bound.
        mib = 1024 * 1024
        usable = 8 * 1024 * mib
        for arch, (measured_fixed_mib, measured_ratio) in self.MEASURED.items():
            with self.subTest(arch=arch):
                measured = measured_fixed_mib * mib + usable * measured_ratio
                reserved = limits.guest_kernel_reserve_bytes(usable, arch=arch)
                self.assertGreater(reserved, measured, f"{arch} under-reserves")
                self.assertLess(reserved, 1.5 * measured, f"{arch} over-reserves")


if __name__ == "__main__":
    unittest.main()

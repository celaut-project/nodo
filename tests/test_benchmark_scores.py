"""This node's per-core benchmark scores (#459): config.yaml, and holding a requirement against them.

* **config** -- `benchmark.BY_ARCH.<arch>` is read per architecture, `-1` and a missing
  key both read as unmeasured, a value written by hand is read as written, and the
  validator refuses what admission could only misread;
* **the comparison** -- a measured score below the requirement is a shortfall, an
  unmeasured one is unenforced, and memory bandwidth only counts over a working set at
  least as large as the one required (the pinned 1 GiB when it names none), never over
  an unknown one.
"""
import copy
import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from src.utils import benchmark  # noqa: E402
from src.utils.benchmark import (  # noqa: E402
    MEM_BANDWIDTH_KEYS,
    MEM_BANDWIDTH_WORKING_SET,
    MEM_WORKING_SETS,
    REMOVED_KEYS,
    SCORE_KEYS,
    UNMEASURED,
    shortfalls,
)
from src.utils.config import ConfigManager  # noqa: E402
from src.utils.config_validation import (  # noqa: E402
    ConfigValidationError,
    validate_benchmark_config,
)

GIB = 1 << 30
MIB = 1 << 20
BW_256 = "mem_bandwidth_256mib_bytes_per_sec"
BW_1GIB = "mem_bandwidth_1gib_bytes_per_sec"


def _with_scores(block):
    """Patch ConfigManager.get so `benchmark.BY_ARCH` answers ``block``."""
    real_get = ConfigManager.get

    def _get(self, key, default=None):
        if key == benchmark.CONFIG_KEY:
            return copy.deepcopy(block)
        return real_get(self, key, default)

    return patch.object(ConfigManager, "get", _get)


class ConfigScoresTests(unittest.TestCase):

    def test_the_shipped_example_is_every_key_unmeasured_for_both_architectures(self):
        self.assertEqual(benchmark.configured_architectures(), ("linux/amd64", "linux/arm64"))
        for arch in ("linux/amd64", "linux/arm64"):
            with self.subTest(arch=arch):
                self.assertEqual(
                    benchmark.node_scores(arch), {key: UNMEASURED for key in SCORE_KEYS}
                )

    def test_a_value_written_by_hand_is_read_as_written(self):
        with _with_scores({"linux/amd64": {"int_ops_per_sec": 900000, "flt_ops_per_sec": -1}}):
            scores = benchmark.node_scores("linux/amd64")
        self.assertEqual(scores["int_ops_per_sec"], 900000)
        self.assertEqual(scores["flt_ops_per_sec"], UNMEASURED)

    def test_a_missing_key_architecture_or_block_is_unmeasured(self):
        unmeasured = {key: UNMEASURED for key in SCORE_KEYS}
        for block in ({}, None, {"linux/amd64": None}, {"linux/arm64": {"int_ops_per_sec": 5}}):
            with self.subTest(block=block), _with_scores(block):
                self.assertEqual(benchmark.node_scores("linux/amd64"), unmeasured)
        self.assertEqual(benchmark.node_scores(None), unmeasured)

    def test_each_architecture_has_its_own_scores(self):
        with _with_scores({
            "linux/amd64": {"int_ops_per_sec": 900000},
            "linux/arm64": {"int_ops_per_sec": 20000},
        }):
            self.assertEqual(benchmark.node_scores("linux/amd64")["int_ops_per_sec"], 900000)
            self.assertEqual(benchmark.node_scores("linux/arm64")["int_ops_per_sec"], 20000)

    def test_measured_drops_the_unmeasured_and_unmeasured_keys_names_them(self):
        scores = {key: UNMEASURED for key in SCORE_KEYS}
        scores["int_ops_per_sec"] = 7
        scores["flt_ops_per_sec"] = 0
        self.assertEqual(benchmark.measured(scores), {"flt_ops_per_sec": 0, "int_ops_per_sec": 7})
        self.assertNotIn("int_ops_per_sec", benchmark.unmeasured_keys(scores))
        self.assertIn(BW_1GIB, benchmark.unmeasured_keys(scores))

    def test_memory_bandwidth_has_one_key_per_working_set(self):
        # The working set is part of the name, so a score is only ever read against a
        # requirement over the same amount of memory.
        self.assertEqual(
            dict(MEM_BANDWIDTH_WORKING_SET),
            {"mem_bandwidth_64mib_bytes_per_sec": 64 * MIB, BW_256: 256 * MIB, BW_1GIB: GIB},
        )
        for key in MEM_BANDWIDTH_KEYS:
            self.assertIn(key, SCORE_KEYS)
        # The largest is what the benchmark service (2 GiB guest) can measure.
        self.assertEqual(max(MEM_WORKING_SETS.values()), GIB)


class ValidateBenchmarkConfigTests(unittest.TestCase):

    def _validate(self, block):
        validate_benchmark_config({"benchmark": {"BY_ARCH": block}})

    def test_absent_and_unmeasured_are_valid(self):
        validate_benchmark_config({})
        validate_benchmark_config({"benchmark": None})
        validate_benchmark_config({"benchmark": {}})
        self._validate({"linux/amd64": {key: -1 for key in SCORE_KEYS}})
        self._validate({"linux/arm64": None})

    def test_manual_values_are_valid(self):
        self._validate({"linux/amd64": {"int_ops_per_sec": 0, BW_1GIB: 10 ** 10, BW_256: 2 * 10 ** 10}})

    def test_the_former_bandwidth_keys_are_refused_naming_the_new_ones(self):
        for old in REMOVED_KEYS:
            with self.subTest(key=old):
                with self.assertRaisesRegex(ConfigValidationError, "no longer exists.*mem_bandwidth_1gib"):
                    self._validate({"linux/amd64": {old: 1}})

    def test_a_value_admission_could_only_misread_is_refused_naming_it(self):
        for bad in (-2, 1.5, "900000", True, None, [1]):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigValidationError) as raised:
                    self._validate({"linux/amd64": {"int_ops_per_sec": bad}})
                self.assertIn("benchmark.BY_ARCH.linux/amd64.int_ops_per_sec", str(raised.exception))

    def test_an_alias_or_unknown_architecture_is_refused(self):
        for arch in ("amd64", "x86_64", "linux/riscv64"):
            with self.subTest(arch=arch):
                with self.assertRaisesRegex(ConfigValidationError, "canonical tag"):
                    self._validate({arch: {"int_ops_per_sec": 1}})

    def test_a_misspelt_key_is_refused(self):
        with self.assertRaisesRegex(ConfigValidationError, "int_ops_per_second"):
            self._validate({"linux/amd64": {"int_ops_per_second": 1}})

    def test_a_stray_setting_or_shape_is_refused(self):
        with self.assertRaises(ConfigValidationError):
            validate_benchmark_config({"benchmark": {"linux/amd64": {}}})
        with self.assertRaises(ConfigValidationError):
            validate_benchmark_config({"benchmark": [1]})
        with self.assertRaises(ConfigValidationError):
            self._validate([1])
        with self.assertRaises(ConfigValidationError):
            self._validate({"linux/amd64": 5})

    def test_the_load_path_runs_it(self):
        # A malformed score must stop the node at load, not at the first admission.
        import os
        import tempfile

        import yaml

        from src.utils.singleton import Singleton

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.example.yaml")) as f:
            config = yaml.safe_load(f)
        config["benchmark"]["BY_ARCH"]["linux/amd64"]["int_ops_per_sec"] = "fast"
        with tempfile.TemporaryDirectory() as tmp:
            config["main"]["MAIN_DIR"] = tmp
            config["main"]["STORAGE"] = os.path.join(tmp, "storage")
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w") as f:
                yaml.safe_dump(config, f)
            saved = Singleton._instances.pop(ConfigManager, None)
            try:
                with self.assertRaisesRegex(ConfigValidationError, "int_ops_per_sec"):
                    ConfigManager(config_path=path).load_config()
            finally:
                Singleton._instances.pop(ConfigManager, None)
                if saved is not None:
                    Singleton._instances[ConfigManager] = saved


def _scores(**measured):
    scores = {key: UNMEASURED for key in SCORE_KEYS}
    scores.update(measured)
    return scores


class ShortfallsTests(unittest.TestCase):
    WHERE = "on this node for linux/amd64"

    def _check(self, required, scores):
        return shortfalls(required, scores, where=self.WHERE)

    def test_a_measured_score_below_the_requirement_is_a_shortfall(self):
        found, unenforced = self._check({"int_ops_per_sec": 500000}, _scores(int_ops_per_sec=20000))
        self.assertEqual(unenforced, {})
        self.assertEqual(len(found), 1)
        self.assertIn("Requested: 500000 per core per second", found[0])
        self.assertIn(f"measured {self.WHERE}: 20000", found[0])

    def test_a_score_at_or_above_it_is_not(self):
        for score in (500000, 900000):
            with self.subTest(score=score):
                self.assertEqual(
                    self._check({"int_ops_per_sec": 500000}, _scores(int_ops_per_sec=score)), ([], {})
                )

    def test_an_unmeasured_score_is_unenforced_not_a_shortfall(self):
        found, unenforced = self._check(
            {"int_ops_per_sec": 2 ** 64 - 1, "sha256_hashes_per_sec": 1}, _scores()
        )
        self.assertEqual(found, [])
        self.assertEqual(unenforced, {"int_ops_per_sec": 2 ** 64 - 1, "sha256_hashes_per_sec": 1})

    def test_an_unrecognised_primitive_is_unenforced(self):
        self.assertEqual(
            self._check({"quantum_ops_per_sec": 3}, _scores(int_ops_per_sec=1)),
            ([], {"quantum_ops_per_sec": 3}),
        )

    def test_every_failing_primitive_is_reported_not_only_the_first(self):
        found, _ = self._check(
            {"int_ops_per_sec": 10, "flt_ops_per_sec": 10},
            _scores(int_ops_per_sec=1, flt_ops_per_sec=1),
        )
        self.assertEqual(len(found), 2)

    def test_a_bandwidth_is_held_against_the_score_over_the_same_working_set(self):
        required = {BW_256: 10 ** 9}
        self.assertEqual(self._check(required, _scores(**{BW_256: 5 * 10 ** 9})), ([], {}))
        found, _ = self._check(required, _scores(**{BW_256: 10 ** 8}))
        self.assertEqual(len(found), 1)
        self.assertIn(BW_256, found[0])
        self.assertIn("Requested: 1000000000", found[0])

    def test_an_unmeasured_working_set_is_read_against_the_next_larger_measured_one(self):
        # A larger working set can only lower a bandwidth, so its score is a safe floor.
        scores = _scores(**{BW_1GIB: 5 * 10 ** 9})
        self.assertEqual(self._check({BW_256: 10 ** 9}, scores), ([], {}))
        found, unenforced = self._check({BW_256: 10 ** 10}, scores)
        self.assertEqual(unenforced, {})
        self.assertEqual(len(found), 1)
        self.assertIn(BW_256, found[0])
        self.assertIn(f"measured {self.WHERE}: 5000000000 (over {BW_1GIB})", found[0])

    def test_the_smallest_larger_working_set_is_the_one_used(self):
        scores = _scores(**{BW_256: 3 * 10 ** 9, BW_1GIB: 10 ** 9})
        # 100 MiB has no key and no score; the next larger measured is 256 MiB, not 1 GiB.
        found, _ = self._check({"mem_bandwidth_100mib_bytes_per_sec": 2 * 10 ** 9}, scores)
        self.assertEqual(found, [])
        found, _ = self._check({"mem_bandwidth_100mib_bytes_per_sec": 4 * 10 ** 9}, scores)
        self.assertIn(f"(over {BW_256})", found[0])

    def test_its_own_score_wins_over_a_larger_one(self):
        scores = _scores(**{BW_256: 10 ** 8, BW_1GIB: 10 ** 12})
        found, _ = self._check({BW_256: 10 ** 9}, scores)
        self.assertEqual(len(found), 1)
        self.assertNotIn("over", found[0])

    def test_a_working_set_larger_than_any_measured_is_unenforced(self):
        # A smaller set would flatter the bandwidth, so it is no evidence either way.
        required = {"mem_bandwidth_4gib_bytes_per_sec": 10 ** 12}
        self.assertEqual(self._check(required, _scores(**{BW_1GIB: 1})), ([], required))

    def test_any_size_is_a_recognised_key_and_a_malformed_one_is_not(self):
        sizes = ("mem_bandwidth_300mib_bytes_per_sec", "mem_bandwidth_8kib_bytes_per_sec",
                 "mem_bandwidth_2gib_bytes_per_sec")
        self.assertEqual(benchmark.unrecognised_keys({key: 1 for key in sizes}), ())
        bad = ("mem_bandwidth_0mib_bytes_per_sec", "mem_bandwidth_mib_bytes_per_sec",
               "mem_bandwidth_5mb_bytes_per_sec", "mem_bandwidth_bytes_per_sec")
        self.assertEqual(benchmark.unrecognised_keys({key: 1 for key in bad}), tuple(sorted(bad)))
        self.assertEqual(benchmark.mem_bandwidth_working_set(sizes[0]), 300 * MIB)

    def test_an_unmeasured_bandwidth_is_unenforced(self):
        self.assertEqual(self._check({BW_1GIB: 1}, _scores()), ([], {BW_1GIB: 1}))

    def test_the_former_bandwidth_keys_are_unrecognised_in_a_requirement(self):
        for old in REMOVED_KEYS:
            self.assertEqual(self._check({old: 1}, _scores()), ([], {old: 1}))
            self.assertEqual(benchmark.unrecognised_keys({old: 1}), (old,))

    def test_parse_benchmark_refuses_the_former_bandwidth_keys_naming_the_new_ones(self):
        for old in REMOVED_KEYS:
            with self.subTest(key=old):
                with self.assertRaisesRegex(ValueError, "no longer exists.*mem_bandwidth_64mib"):
                    benchmark.parse_benchmark({old: 1}, "resources.at_init.benchmark")
        self.assertEqual(
            benchmark.parse_benchmark({BW_256: 3}, "resources.at_init.benchmark"), {BW_256: 3}
        )


if __name__ == "__main__":
    unittest.main()

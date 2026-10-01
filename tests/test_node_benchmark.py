"""This node's own benchmark scores (#452): parsing, the cache, and the comparison.

Three things are pinned here, one class each:

* **the serial log** -- what counts as a measurement, and what only looks like one;
* **the cache** -- per architecture, atomic, and never a reason for admission to raise;
* **the comparison** -- a shortfall only where a recognised primitive was measured
  below the requirement, silence everywhere else.

The guest boot that produces the scores is tested in test_node_benchmark_boot.py.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

# Before anything that builds a ConfigManager at import: the shipped example points
# STORAGE at /nodo, which only exists on an installed node.
load_example_config()

from src.utils import node_benchmark  # noqa: E402
from src.utils.min_benchmark import MIN_BENCHMARK_KEYS  # noqa: E402
from src.utils.node_benchmark import (  # noqa: E402
    benchmark_shortfalls,
    forget_node_benchmark,
    get_cached_node_benchmark,
    parse_benchmark_serial_output,
    read_node_benchmark_cache,
    write_node_benchmark_cache,
)
from tests.test_ch_initramfs_builder import BENCHMARK_KEYS  # noqa: E402

GUEST_LOG = """\
[    0.512345] Run /init as init process
+ benchmark_requested
+ echo '[nodo-benchmark] int_ops_per_sec=1'
[nodo-benchmark] begin
[nodo-benchmark] int_ops_per_sec=2400000\r
[nodo-benchmark] flt_ops_per_sec=9100000
[nodo-benchmark] skipped mem_bandwidth_bytes_per_sec: busybox has no dd applet
[nodo-benchmark] sha256_hashes_per_sec=410000
[nodo-benchmark] done
[   10.912345] reboot: Power down
"""


class ParseSerialOutputTests(unittest.TestCase):
    def test_reads_every_tagged_line(self):
        self.assertEqual(parse_benchmark_serial_output(GUEST_LOG), {
            "int_ops_per_sec": 2400000,
            "flt_ops_per_sec": 9100000,
            "sha256_hashes_per_sec": 410000,
        })

    def test_a_trace_of_the_echo_is_not_a_measurement(self):
        # /init runs under `set -x` until the branch turns it off; the trace line
        # quotes the tag, and must not be read as int_ops_per_sec=1.
        self.assertNotEqual(parse_benchmark_serial_output(GUEST_LOG)["int_ops_per_sec"], 1)
        self.assertEqual(parse_benchmark_serial_output("+ echo '[nodo-benchmark] x=1'\n"), {})

    def test_a_skipped_primitive_is_absent_not_zero(self):
        self.assertNotIn("mem_bandwidth_bytes_per_sec", parse_benchmark_serial_output(GUEST_LOG))

    def test_an_unrecognised_primitive_is_kept(self):
        self.assertEqual(parse_benchmark_serial_output("[nodo-benchmark] quantum_ops_per_sec=3\n"),
                         {"quantum_ops_per_sec": 3})

    def test_what_is_not_a_non_negative_integer_is_ignored(self):
        text = ("[nodo-benchmark] a=-1\n[nodo-benchmark] b=1.5\n[nodo-benchmark] c=\n"
                f"[nodo-benchmark] d={2 ** 64}\n[nodo-benchmark] e=7 trailing\n")
        self.assertEqual(parse_benchmark_serial_output(text), {})

    def test_the_last_value_wins(self):
        text = "[nodo-benchmark] int_ops_per_sec=1\n[nodo-benchmark] int_ops_per_sec=2\n"
        self.assertEqual(parse_benchmark_serial_output(text), {"int_ops_per_sec": 2})

    def test_nothing_in_nothing_out(self):
        self.assertEqual(parse_benchmark_serial_output(""), {})
        self.assertEqual(parse_benchmark_serial_output(None), {})

    def test_the_guest_prints_exactly_the_primitives_this_node_names(self):
        # The initramfs tests spell the keys out (they run without protobuf); this is
        # where that spelling is held to MIN_BENCHMARK_KEYS.
        self.assertEqual(tuple(BENCHMARK_KEYS), tuple(MIN_BENCHMARK_KEYS))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "sub", node_benchmark.CACHE_FILE_NAME)

    def tearDown(self):
        self._dir.cleanup()

    def test_round_trip_per_architecture(self):
        write_node_benchmark_cache({"int_ops_per_sec": 5}, arch="linux/amd64",
                                   virtualizer="ch", fingerprint="f1", path=self.path)
        write_node_benchmark_cache({"int_ops_per_sec": 1}, arch="linux/arm64",
                                   virtualizer="qemu", fingerprint="f2", path=self.path)
        self.assertEqual(get_cached_node_benchmark("linux/amd64", path=self.path), {"int_ops_per_sec": 5})
        self.assertEqual(get_cached_node_benchmark("linux/arm64", path=self.path), {"int_ops_per_sec": 1})
        entry = read_node_benchmark_cache(self.path)["linux/arm64"]
        self.assertEqual((entry["virtualizer"], entry["fingerprint"]), ("qemu", "f2"))
        self.assertIsInstance(entry["measured_at"], int)

    def test_omitted_arch_is_the_hosts(self):
        write_node_benchmark_cache({"int_ops_per_sec": 5}, arch="linux/riscv", virtualizer="ch", path=self.path)
        with patch.object(node_benchmark, "host_arch_tag", return_value="linux/riscv"):
            self.assertEqual(get_cached_node_benchmark(path=self.path), {"int_ops_per_sec": 5})

    def test_an_arch_never_measured_is_empty(self):
        write_node_benchmark_cache({"int_ops_per_sec": 5}, arch="linux/amd64", virtualizer="ch", path=self.path)
        self.assertEqual(get_cached_node_benchmark("linux/arm64", path=self.path), {})

    def test_never_raises(self):
        os.makedirs(self.path)  # a directory where the file should be
        broken = os.path.join(self._dir.name, "broken.json")
        cases = {
            "missing": os.path.join(self._dir.name, "absent.json"),
            "a directory": self.path,
            "not json": ("{", broken),
            "a list": ("[]", broken),
            "another format": (json.dumps({"format": 999, "archs": {}}), broken),
            "archs not a map": (json.dumps({"format": node_benchmark.CACHE_FORMAT, "archs": []}), broken),
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                if isinstance(case, tuple):
                    Path(case[1]).write_text(case[0], encoding="utf-8")
                    case = case[1]
                self.assertEqual(get_cached_node_benchmark("linux/amd64", path=case), {})
                self.assertEqual(read_node_benchmark_cache(case), {})

    def test_never_raises_without_a_configured_cache_or_a_host_arch(self):
        with patch.object(node_benchmark, "default_cache_path", return_value=None):
            self.assertEqual(get_cached_node_benchmark("linux/amd64"), {})
        with patch.object(node_benchmark, "host_arch_tag", side_effect=RuntimeError("boom")):
            self.assertEqual(get_cached_node_benchmark(path=self.path), {})
        with patch.object(node_benchmark, "host_arch_tag", return_value=None):
            self.assertEqual(get_cached_node_benchmark(path=self.path), {})

    def test_only_non_negative_integers_survive_a_read(self):
        Path(self.path).parent.mkdir(parents=True)
        Path(self.path).write_text(json.dumps({"format": node_benchmark.CACHE_FORMAT, "archs": {
            "linux/amd64": {"scores": {"int_ops_per_sec": 5, "flt_ops_per_sec": True,
                                       "sha256_hashes_per_sec": -1, "mem_bandwidth_bytes_per_sec": "9",
                                       "": 3}},
            "linux/arm64": "not an entry",
        }}), encoding="utf-8")
        self.assertEqual(get_cached_node_benchmark("linux/amd64", path=self.path), {"int_ops_per_sec": 5})
        self.assertNotIn("linux/arm64", read_node_benchmark_cache(self.path))

    def test_forget_drops_only_the_named_architectures(self):
        for arch in ("linux/amd64", "linux/arm64"):
            write_node_benchmark_cache({"int_ops_per_sec": 1}, arch=arch, virtualizer="ch", path=self.path)
        forget_node_benchmark(["linux/arm64"], path=self.path)
        self.assertEqual(set(read_node_benchmark_cache(self.path)), {"linux/amd64"})

    def test_a_write_leaves_no_temporary_file_behind(self):
        write_node_benchmark_cache({"int_ops_per_sec": 1}, arch="linux/amd64", virtualizer="ch", path=self.path)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [node_benchmark.CACHE_FILE_NAME])

    def test_writing_without_a_configured_cache_is_an_error_the_caller_sees(self):
        with patch.object(node_benchmark, "default_cache_path", return_value=None):
            with self.assertRaises(ValueError):
                write_node_benchmark_cache({}, arch="linux/amd64", virtualizer="ch")


class ShortfallTests(unittest.TestCase):
    def test_measured_below_the_requirement_is_a_shortfall(self):
        shortfalls = benchmark_shortfalls({"int_ops_per_sec": 500}, {"int_ops_per_sec": 499})
        self.assertEqual(len(shortfalls), 1)
        self.assertIn("resources.at_most.min_benchmark.int_ops_per_sec", shortfalls[0])
        self.assertIn("Requested: 500 per core per second, measured on this node: 499.", shortfalls[0])

    def test_meeting_the_requirement_exactly_is_enough(self):
        self.assertEqual(benchmark_shortfalls({"int_ops_per_sec": 500}, {"int_ops_per_sec": 500}), [])

    def test_a_zero_requirement_is_no_requirement(self):
        self.assertEqual(benchmark_shortfalls({"int_ops_per_sec": 0}, {"int_ops_per_sec": 0}), [])

    def test_an_unmeasured_primitive_is_silent(self):
        # Unknown is not insufficient: nothing measured means nothing to refuse on.
        self.assertEqual(benchmark_shortfalls({"int_ops_per_sec": 2 ** 64 - 1}, {}), [])
        self.assertEqual(
            benchmark_shortfalls({"int_ops_per_sec": 9, "flt_ops_per_sec": 9}, {"flt_ops_per_sec": 9}),
            [],
        )

    def test_an_unrecognised_primitive_is_silent_even_if_a_score_exists(self):
        self.assertEqual(benchmark_shortfalls({"quantum_ops_per_sec": 9}, {"quantum_ops_per_sec": 1}), [])

    def test_every_shortfall_is_reported_in_key_order(self):
        shortfalls = benchmark_shortfalls(
            {"sha256_hashes_per_sec": 9, "int_ops_per_sec": 9, "flt_ops_per_sec": 1},
            {"sha256_hashes_per_sec": 1, "int_ops_per_sec": 1, "flt_ops_per_sec": 1},
        )
        self.assertEqual(len(shortfalls), 2)
        self.assertIn(".int_ops_per_sec.", shortfalls[0])
        self.assertIn(".sha256_hashes_per_sec.", shortfalls[1])



if __name__ == "__main__":
    unittest.main()

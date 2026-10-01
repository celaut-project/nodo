"""Booting this node's guest to measure it (#452): `src.virtualizers.microvm.benchmark`.

* **the wait** -- a benchmark guest ends on its own, and every other way it can end is
  a failed measurement with the hypervisor stopped;
* **the boot** -- no disk, no network, one vCPU, the benchmark token on the cmdline,
  and no boot at all for an initramfs that predates the branch;
* **when it runs** -- only for what changed since the last measurement, unless forced,
  one at a time, and never in a way that can reach the thread that started it.
"""
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
from src.utils.node_benchmark import (  # noqa: E402
    get_cached_node_benchmark,
    parse_benchmark_serial_output,
    read_node_benchmark_cache,
    write_node_benchmark_cache,
)
from tests.test_node_benchmark import GUEST_LOG  # noqa: E402


class _FakeProcess:
    """Popen's poll/terminate/kill/wait, with an exit that happens when told to."""

    def __init__(self, exits_at_poll=None, returncode=0):
        self.polls = 0
        self.exits_at_poll = exits_at_poll
        self.returncode = None
        self._code = returncode
        self.terminated = False

    def poll(self):
        self.polls += 1
        if self.exits_at_poll is not None and self.polls >= self.exits_at_poll:
            self.returncode = self._code
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


try:
    from src.virtualizers.microvm import benchmark as microvm_benchmark
except Exception as import_exc:  # pragma: no cover - environment-dependent
    microvm_benchmark = None
    BENCHMARK_IMPORT_ERROR = import_exc
else:
    BENCHMARK_IMPORT_ERROR = None


@unittest.skipIf(microvm_benchmark is None, f"benchmark module unavailable: {BENCHMARK_IMPORT_ERROR}")
class WaitForBenchmarkTests(unittest.TestCase):
    """The new wait: "is it over?", where every other wait asks "is it up yet?"."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.log = Path(self._dir.name) / "serial.log"
        self.now = 0.0

    def tearDown(self):
        self._dir.cleanup()

    def _clock(self):
        return self.now

    def _sleep(self, seconds):
        self.now += seconds

    def _wait(self, process, **kwargs):
        return microvm_benchmark.wait_for_benchmark(
            process, self.log, timeout=kwargs.pop("timeout", 30.0), done_grace=kwargs.pop("done_grace", 5.0),
            poll_interval=1.0, clock=self._clock, sleep=self._sleep,
        )

    def test_a_guest_that_powers_off_after_done_is_a_measurement(self):
        self.log.write_text(GUEST_LOG, encoding="utf-8")
        process = _FakeProcess(exits_at_poll=3)
        text = self._wait(process)
        self.assertEqual(parse_benchmark_serial_output(text), parse_benchmark_serial_output(GUEST_LOG))
        self.assertFalse(process.terminated)

    def test_a_guest_that_cannot_power_off_is_stopped_after_the_grace(self):
        self.log.write_text(GUEST_LOG, encoding="utf-8")
        process = _FakeProcess()
        self.assertIn(node_benchmark.DONE_LINE, self._wait(process, done_grace=5.0))
        self.assertTrue(process.terminated)
        self.assertGreaterEqual(self.now, 5.0)
        self.assertLess(self.now, 30.0)

    def test_a_guest_that_never_finishes_times_out_and_is_stopped(self):
        self.log.write_text("[nodo-benchmark] begin\n", encoding="utf-8")
        process = _FakeProcess()
        with self.assertRaises(microvm_benchmark.BenchmarkFailed) as caught:
            self._wait(process, timeout=30.0)
        self.assertIn("no result within 30s", str(caught.exception))
        self.assertTrue(process.terminated)
        self.assertGreaterEqual(self.now, 30.0)

    def test_an_exit_without_done_is_a_failure(self):
        self.log.write_text("[nodo-benchmark] int_ops_per_sec=5\nKernel panic\n", encoding="utf-8")
        with self.assertRaises(microvm_benchmark.BenchmarkFailed) as caught:
            self._wait(_FakeProcess(exits_at_poll=1, returncode=1))
        self.assertIn("exited with code 1", str(caught.exception))

    def test_an_initramfs_without_the_branch_fails_fast(self):
        # An older /init ignores nodo.benchmark=1, looks for /dev/vda, and parks in
        # its fatal loop: the hypervisor stays up and the guest never finishes.
        self.log.write_text("[nodo-ch-initramfs] ERROR: timed out waiting for /dev/vda after 20s\n",
                            encoding="utf-8")
        process = _FakeProcess()
        with self.assertRaises(microvm_benchmark.BenchmarkFailed) as caught:
            self._wait(process, timeout=300.0)
        self.assertIn("initramfs gave up", str(caught.exception))
        self.assertTrue(process.terminated)
        self.assertLess(self.now, 300.0)

    def test_a_missing_serial_log_is_waited_on_not_raised(self):
        process = _FakeProcess()
        with self.assertRaises(microvm_benchmark.BenchmarkFailed):
            self._wait(process, timeout=3.0)
        self.assertTrue(process.terminated)


@unittest.skipIf(microvm_benchmark is None, f"benchmark module unavailable: {BENCHMARK_IMPORT_ERROR}")
class BootCommandTests(unittest.TestCase):
    def _target(self, virtualizer, arch="linux/amd64"):
        return microvm_benchmark.Target(arch, virtualizer, "/bin/hv", "/k/vmlinuz", "/k/initramfs")

    def test_ch_boots_the_guest_with_no_disk_no_network_and_one_vcpu(self):
        command = microvm_benchmark.ch_command(self._target("ch"), Path("/r/serial.log"))
        self.assertEqual(command[0], "/bin/hv")
        self.assertNotIn("--disk", command)
        self.assertNotIn("--net", command)
        self.assertEqual(command[command.index("--cpus") + 1], "boot=1")
        self.assertIn("nodo.benchmark=1", command[command.index("--cmdline") + 1])
        self.assertEqual(command[command.index("--serial") + 1], "file=/r/serial.log")
        self.assertEqual(command[command.index("--initramfs") + 1], "/k/initramfs")

    def test_qemu_boots_it_under_tcg_and_exits_on_any_reset(self):
        command = microvm_benchmark.qemu_command(self._target("qemu", "linux/arm64"), Path("/r/serial.log"))
        self.assertEqual(command[command.index("-accel") + 1], "tcg")
        self.assertEqual(command[command.index("-smp") + 1], "1")
        self.assertIn("-no-reboot", command)
        self.assertNotIn("-drive", command)
        self.assertEqual(command[command.index("-nic") + 1], "none")
        append = command[command.index("-append") + 1]
        self.assertIn("console=ttyAMA0", append)
        self.assertIn("nodo.benchmark=1", append)

    def test_a_guest_without_the_capability_is_not_booted(self):
        with patch.object(microvm_benchmark.microvm_initramfs, "benchmark_capability", return_value=""), \
                patch.object(microvm_benchmark.subprocess, "Popen") as popen:
            with self.assertRaises(microvm_benchmark.BenchmarkFailed) as caught:
                microvm_benchmark.measure(self._target("ch"))
        popen.assert_not_called()
        self.assertIn("has no benchmark branch", str(caught.exception))


@unittest.skipIf(microvm_benchmark is None, f"benchmark module unavailable: {BENCHMARK_IMPORT_ERROR}")
class RefreshTests(unittest.TestCase):
    """When a measurement runs, and what happens to the cache when it does or fails."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, node_benchmark.CACHE_FILE_NAME)
        self.target = microvm_benchmark.Target("linux/amd64", "ch", "/bin/hv", "/k/vmlinuz", "/k/initramfs")
        self._patches = [
            patch.object(node_benchmark, "default_cache_path", return_value=self.path),
            patch.object(microvm_benchmark, "served_targets", return_value=[self.target]),
            patch.object(microvm_benchmark.log, "LOGGER"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._dir.cleanup()

    def _refresh(self, fingerprint, measured, **kwargs):
        with patch.object(microvm_benchmark, "fingerprint", return_value=fingerprint), \
                patch.object(microvm_benchmark, "measure", **measured) as measure:
            outcomes = microvm_benchmark.refresh(**kwargs)
        return outcomes, measure

    def test_first_start_measures_and_records(self):
        outcomes, _ = self._refresh("f1", {"return_value": {"int_ops_per_sec": 7}})
        self.assertTrue(outcomes["linux/amd64"].startswith("measured"))
        self.assertEqual(get_cached_node_benchmark("linux/amd64"), {"int_ops_per_sec": 7})

    def test_an_unchanged_node_boots_nothing(self):
        self._refresh("f1", {"return_value": {"int_ops_per_sec": 7}})
        outcomes, measure = self._refresh("f1", {"return_value": {"int_ops_per_sec": 1}})
        measure.assert_not_called()
        self.assertTrue(outcomes["linux/amd64"].startswith("unchanged"))

    def test_force_measures_anyway(self):
        self._refresh("f1", {"return_value": {"int_ops_per_sec": 7}})
        _, measure = self._refresh("f1", {"return_value": {"int_ops_per_sec": 8}}, force=True)
        measure.assert_called_once()
        self.assertEqual(get_cached_node_benchmark("linux/amd64"), {"int_ops_per_sec": 8})

    def test_a_failed_re_measure_of_the_same_guest_keeps_the_old_scores(self):
        self._refresh("f1", {"return_value": {"int_ops_per_sec": 7}})
        outcomes, _ = self._refresh(
            "f1", {"side_effect": microvm_benchmark.BenchmarkFailed("timeout")}, force=True)
        self.assertTrue(outcomes["linux/amd64"].startswith("failed"))
        self.assertEqual(get_cached_node_benchmark("linux/amd64"), {"int_ops_per_sec": 7})

    def test_a_changed_guest_drops_the_old_scores_before_measuring(self):
        # Scores of another kernel or hypervisor are not this node's: if the new
        # measurement fails, the architecture is unmeasured, not stale.
        self._refresh("f1", {"return_value": {"int_ops_per_sec": 7}})
        self._refresh("f2", {"side_effect": microvm_benchmark.BenchmarkFailed("timeout")})
        self.assertEqual(get_cached_node_benchmark("linux/amd64"), {})

    def test_an_architecture_no_longer_served_is_forgotten(self):
        write_node_benchmark_cache({"int_ops_per_sec": 1}, arch="linux/arm64", virtualizer="qemu")
        self._refresh("f1", {"return_value": {"int_ops_per_sec": 7}})
        self.assertEqual(set(read_node_benchmark_cache()), {"linux/amd64"})

    def test_a_second_measurement_does_not_start_while_one_runs(self):
        import fcntl

        with open(self.path + ".lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            outcomes, measure = self._refresh("f1", {"return_value": {"int_ops_per_sec": 7}})
        self.assertEqual(outcomes, {})
        measure.assert_not_called()

    def test_the_background_start_never_raises(self):
        with patch.object(microvm_benchmark, "refresh", side_effect=RuntimeError("boom")):
            thread = microvm_benchmark.start_background_refresh()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(thread.daemon)


if __name__ == "__main__":
    unittest.main()

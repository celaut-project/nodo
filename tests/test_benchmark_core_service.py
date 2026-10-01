"""The optional `benchmark` core service's startup step (#459).

Launching, asking and stopping are injected, so what is pinned here is the decision
around them, against a real config.yaml (a temporary copy of the example):

* nothing is launched when `core_services.benchmark` is not configured, when every score
  of a served architecture is already written, or for an architecture this node does
  not run;
* what the service answers lands in `benchmark.BY_ARCH.<arch>` -- only in the `-1`s, the
  bandwidth only together with its working set -- under the architecture it ran under;
* every failure leaves the `-1`s as they were, still stops what was launched, and never
  raises.
"""
import json
import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.core_services import benchmark as core  # noqa: E402
from src.utils import benchmark  # noqa: E402
from src.utils.benchmark import MEM_BANDWIDTH_KEY, MEM_WORKING_SET_KEY, UNMEASURED  # noqa: E402
from src.utils.config import ConfigManager  # noqa: E402

GIB = 1 << 30

ANSWER = {
    "architecture": "linux/amd64",
    "int_ops_per_sec": 900000,
    "flt_ops_per_sec": 6000000,
    MEM_BANDWIDTH_KEY: 7000000000,
    MEM_WORKING_SET_KEY: GIB,
    "sha256_hashes_per_sec": 320000,
    "skipped": [],
}


def _instance(token="inst-1", ip="10.0.0.9", port=3030):
    instance = celaut.ServiceInstance(token=token)
    instance.instance.uri_slot.add(internal_port=3030).uri.add(ip=ip, port=port)
    return instance


class _Fakes:
    """launch / get / release doubles that record what they were asked."""

    def __init__(self, answers=None, launch_error=None, get_error=None):
        self.answers = list(answers or [ANSWER])
        self.launch_error = launch_error
        self.get_error = get_error
        self.launched, self.asked, self.released = [], [], []

    def launch(self, service_id):
        self.launched.append(service_id)
        if self.launch_error:
            raise self.launch_error
        return _instance(token=f"inst-{len(self.launched)}")

    def get(self, endpoint, working_set):
        self.asked.append((endpoint, working_set))
        if self.get_error:
            raise self.get_error
        return json.dumps(self.answers.pop(0)).encode()

    def release(self, instance):
        self.released.append(instance.token)

    def run(self, served=("linux/amd64",)):
        return core.measure_missing(
            served=list(served), launch=self.launch, get=self.get, release=self.release
        )


class _ConfiguredNode(unittest.TestCase):
    """A fresh example config per test, with the benchmark core service set."""

    SERVICE_IDS = "aa" * 32

    def setUp(self):
        load_example_config()
        if self.SERVICE_IDS is not None:
            ConfigManager().set("core_services.benchmark", self.SERVICE_IDS)
        patcher = patch.object(core, "declared_architecture", return_value=None)
        self.declared = patcher.start()
        self.addCleanup(patcher.stop)
        # core._env_manager is the ConfigManager singleton of the first load; point it
        # at this test's.
        env = patch.object(core, "_env_manager", ConfigManager())
        env.start()
        self.addCleanup(env.stop)

    def scores(self, arch="linux/amd64"):
        return benchmark.node_scores(arch)


class NotConfiguredTests(_ConfiguredNode):
    SERVICE_IDS = None

    def test_the_shipped_placeholder_measures_nothing(self):
        fakes = _Fakes()
        self.assertEqual(core.configured_service_ids(), [])
        self.assertEqual(fakes.run(), {})
        self.assertEqual(fakes.launched, [])
        self.assertIsNone(core.start_in_background())


class ConfiguredIdsTests(unittest.TestCase):
    def _ids(self, value):
        with patch.object(core._env_manager, "get", return_value={"benchmark": value}):
            return core.configured_service_ids()

    def test_one_id_or_a_list_of_them(self):
        self.assertEqual(self._ids("abc"), ["abc"])
        self.assertEqual(self._ids(["abc", " def ", "abc"]), ["abc", "def"])

    def test_placeholder_empty_or_absent_is_not_configured(self):
        for value in ("<SET_ME>", "", None, ["<SET_ME>", ""]):
            with self.subTest(value=value):
                self.assertEqual(self._ids(value), [])


class MeasureMissingTests(_ConfiguredNode):

    def test_every_unmeasured_score_is_filled_from_the_answer(self):
        fakes = _Fakes()
        written = fakes.run()
        self.assertEqual(fakes.launched, [self.SERVICE_IDS])
        self.assertEqual(fakes.asked, [(("10.0.0.9", 3030), GIB)])
        self.assertEqual(fakes.released, ["inst-1"])
        self.assertEqual(sorted(written["linux/amd64"]), sorted(benchmark.SCORE_KEYS))
        scores = self.scores()
        for key in benchmark.SCORE_KEYS:
            self.assertEqual(scores[key], ANSWER[key])
        # The other architecture is untouched.
        self.assertEqual(self.scores("linux/arm64")["int_ops_per_sec"], UNMEASURED)

    def test_it_is_written_to_config_yaml_itself(self):
        import yaml

        _Fakes().run()
        with open(ConfigManager().config_path) as f:
            on_disk = yaml.safe_load(f)["benchmark"]["BY_ARCH"]["linux/amd64"]
        self.assertEqual(on_disk["int_ops_per_sec"], 900000)

    def test_a_value_written_by_hand_is_never_overwritten(self):
        ConfigManager().set("benchmark.BY_ARCH.linux/amd64.int_ops_per_sec", 123)
        _Fakes().run()
        scores = self.scores()
        self.assertEqual(scores["int_ops_per_sec"], 123)
        self.assertEqual(scores["flt_ops_per_sec"], 6000000)

    def test_nothing_is_launched_when_every_primitive_is_written(self):
        ConfigManager().set("benchmark.BY_ARCH.linux/amd64", {
            "int_ops_per_sec": 1, "flt_ops_per_sec": 1, MEM_BANDWIDTH_KEY: 1,
            MEM_WORKING_SET_KEY: GIB, "sha256_hashes_per_sec": 1,
        })
        fakes = _Fakes()
        self.assertEqual(fakes.run(), {})
        self.assertEqual(fakes.launched, [])

    def test_an_architecture_this_node_does_not_run_is_not_measured(self):
        fakes = _Fakes()
        self.assertEqual(fakes.run(served=["linux/riscv64"]), {})
        self.assertEqual(fakes.launched, [])

    def test_a_working_set_written_by_hand_is_what_the_service_is_asked_for(self):
        ConfigManager().set("benchmark.BY_ARCH.linux/amd64.mem_bandwidth_working_set_bytes", 2 * GIB)
        answer = dict(ANSWER, **{MEM_WORKING_SET_KEY: 2 * GIB})
        fakes = _Fakes(answers=[answer])
        fakes.run()
        self.assertEqual(fakes.asked[0][1], 2 * GIB)
        self.assertEqual(self.scores()[MEM_WORKING_SET_KEY], 2 * GIB)

    def test_a_bandwidth_without_its_working_set_is_not_written(self):
        answer = {k: v for k, v in ANSWER.items() if k != MEM_WORKING_SET_KEY}
        _Fakes(answers=[answer]).run()
        scores = self.scores()
        self.assertEqual(scores[MEM_BANDWIDTH_KEY], UNMEASURED)
        self.assertEqual(scores["int_ops_per_sec"], 900000)

    def test_a_skipped_primitive_stays_unmeasured(self):
        answer = {k: v for k, v in ANSWER.items() if k != "sha256_hashes_per_sec"}
        answer["skipped"] = ["sha256_hashes_per_sec: sha256sum failed"]
        _Fakes(answers=[answer]).run()
        self.assertEqual(self.scores()["sha256_hashes_per_sec"], UNMEASURED)

    def test_the_answer_is_filed_under_the_architecture_it_ran_under(self):
        answer = dict(ANSWER, architecture="aarch64")
        _Fakes(answers=[answer]).run(served=["linux/amd64", "linux/arm64"])
        self.assertEqual(self.scores("linux/arm64")["int_ops_per_sec"], 900000)
        self.assertEqual(self.scores("linux/amd64")["int_ops_per_sec"], UNMEASURED)

    def test_an_answer_contradicting_the_declared_architecture_is_discarded(self):
        self.declared.return_value = "linux/arm64"
        fakes = _Fakes()  # answers linux/amd64
        fakes.run(served=["linux/amd64", "linux/arm64"])
        self.assertEqual(fakes.released, ["inst-1"])
        self.assertEqual(self.scores("linux/amd64")["int_ops_per_sec"], UNMEASURED)
        self.assertEqual(self.scores("linux/arm64")["int_ops_per_sec"], UNMEASURED)

    def test_an_id_declaring_a_complete_architecture_is_not_launched(self):
        self.declared.return_value = "linux/arm64"
        fakes = _Fakes()
        fakes.run(served=["linux/amd64"])
        self.assertEqual(fakes.launched, [])

    def test_one_id_per_architecture(self):
        ConfigManager().set("core_services.benchmark", ["amd64-id", "arm64-id"])
        self.declared.side_effect = lambda sid: {"amd64-id": "linux/amd64", "arm64-id": "linux/arm64"}[sid]
        fakes = _Fakes(answers=[ANSWER, dict(ANSWER, architecture="linux/arm64", int_ops_per_sec=20000)])
        written = fakes.run(served=["linux/amd64", "linux/arm64"])
        self.assertEqual(fakes.launched, ["amd64-id", "arm64-id"])
        self.assertEqual(set(written), {"linux/amd64", "linux/arm64"})
        self.assertEqual(self.scores("linux/arm64")["int_ops_per_sec"], 20000)
        self.assertEqual(self.scores("linux/amd64")["int_ops_per_sec"], 900000)


class FailureTests(_ConfiguredNode):

    def _unchanged(self):
        self.assertEqual(self.scores(), {k: UNMEASURED for k in benchmark.SCORE_KEYS})

    def test_a_launch_that_fails_writes_nothing_and_does_not_raise(self):
        fakes = _Fakes(launch_error=RuntimeError("no gateway"))
        self.assertEqual(fakes.run(), {})
        self.assertEqual(fakes.released, [])
        self._unchanged()

    def test_a_service_that_never_answers_is_still_stopped(self):
        fakes = _Fakes(get_error=TimeoutError("never accepted a connection"))
        self.assertEqual(fakes.run(), {})
        self.assertEqual(fakes.released, ["inst-1"])
        self._unchanged()

    def test_a_malformed_answer_is_still_stopped(self):
        for body in ([1, 2], {"architecture": "linux/amd64", "int_ops_per_sec": "fast"},
                     {"int_ops_per_sec": 5}):
            with self.subTest(body=body):
                fakes = _Fakes(answers=[body])
                fakes.run()
                self.assertEqual(fakes.released, ["inst-1"])
                self._unchanged()

    def test_a_failing_stop_does_not_lose_the_measurement(self):
        fakes = _Fakes()
        fakes.release = lambda instance: (_ for _ in ()).throw(RuntimeError("already gone"))
        fakes.run()
        self.assertEqual(self.scores()["int_ops_per_sec"], 900000)

    def test_a_second_run_while_one_is_going_does_nothing(self):
        self.assertTrue(core._RUN_LOCK.acquire(blocking=False))
        try:
            fakes = _Fakes()
            self.assertEqual(fakes.run(), {})
            self.assertEqual(fakes.launched, [])
        finally:
            core._RUN_LOCK.release()


class PureHelpersTests(unittest.TestCase):

    def test_parse_answer_keeps_scores_and_canonicalises_the_architecture(self):
        arch, scores = core.parse_answer(json.dumps(dict(
            ANSWER, architecture="x86_64", flt_ops_per_sec=-3, sha256_hashes_per_sec=True,
            future_ops_per_sec=1,
        )).encode())
        self.assertEqual(arch, "linux/amd64")
        self.assertNotIn("flt_ops_per_sec", scores)
        self.assertNotIn("sha256_hashes_per_sec", scores)
        self.assertNotIn("future_ops_per_sec", scores)
        self.assertEqual(scores["int_ops_per_sec"], 900000)

    def test_parse_answer_refuses_what_is_not_an_object(self):
        with self.assertRaises(ValueError):
            core.parse_answer(b"[1]")
        with self.assertRaises(ValueError):
            core.parse_answer(b"not json")

    def test_an_unknown_architecture_is_none(self):
        self.assertIsNone(core.parse_answer(b'{"architecture": "linux/mips"}')[0])

    def test_merge_fills_only_the_unmeasured(self):
        current = {k: UNMEASURED for k in benchmark.SCORE_KEYS}
        current["int_ops_per_sec"] = 5
        merged, filled = core.merge_scores(current, {"int_ops_per_sec": 9, "flt_ops_per_sec": 7})
        self.assertEqual(merged["int_ops_per_sec"], 5)
        self.assertEqual(merged["flt_ops_per_sec"], 7)
        self.assertEqual(filled, ["flt_ops_per_sec"])

    def test_merge_leaves_a_hand_written_bandwidth_and_its_missing_set_alone(self):
        current = {k: UNMEASURED for k in benchmark.SCORE_KEYS}
        current[MEM_BANDWIDTH_KEY] = 10
        merged, filled = core.merge_scores(current, {MEM_BANDWIDTH_KEY: 99, MEM_WORKING_SET_KEY: GIB})
        self.assertEqual((merged[MEM_BANDWIDTH_KEY], merged[MEM_WORKING_SET_KEY]), (10, UNMEASURED))
        self.assertEqual(filled, [])

    def test_the_endpoint_is_the_first_address(self):
        self.assertEqual(core.instance_endpoint(_instance()), ("10.0.0.9", 3030))
        self.assertIsNone(core.instance_endpoint(celaut.ServiceInstance()))


if __name__ == "__main__":
    unittest.main()

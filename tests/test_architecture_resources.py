"""Per-architecture resources on `Peer` and the peer pre-filter (#459, absorbing #454).

* **announcing** -- one entry per architecture this node serves, ceilings capped by
  `host_limits`, only measured benchmark scores, nothing it does not know;
* **signing** -- `Peer.resources` is under the signature, so a relay can neither raise a
  score nor drop an architecture from a claim that still verifies, and the last
  announcement a peer sent is what is stored and read back;
* **the pre-filter** -- a peer whose own announcement rules the request out (architecture
  absent, a limit above its ceiling, a required benchmark above its score) is not asked
  GetServiceEstimatedCost / GetResourceAvailability; a peer that announced nothing is.
"""
import unittest
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from protos import celaut_pb2 as celaut  # noqa: E402
from src.utils import benchmark, host_limits, keyvalue  # noqa: E402
from src.utils.cost_functions import architecture_resources as ar  # noqa: E402

GIB = 1 << 30
AMD64 = ["linux/amd64", "amd64", "x86_64"]
ARM64 = ["linux/arm64", "arm64", "aarch64"]


def _scores(**measured):
    scores = {key: benchmark.UNMEASURED for key in benchmark.SCORE_KEYS}
    scores.update(measured)
    return scores


class AnnouncedResourcesTests(unittest.TestCase):

    def _announce(self, served=(AMD64,), totals=(8, 16 * GIB, 500 * GIB), caps=None, scores=None):
        with patch.object(host_limits, "host_totals", return_value=totals), \
                patch.object(host_limits, "ceilings", return_value=caps), \
                patch.object(benchmark, "node_scores", side_effect=lambda arch: (scores or {}).get(arch, _scores())):
            return ar.announced_resources(served=list(served))

    def test_one_entry_per_served_architecture_with_its_aliases(self):
        entries = self._announce(served=(AMD64, ARM64))
        self.assertEqual([list(e.architecture.tags) for e in entries], [AMD64, ARM64])

    def test_the_ceilings_are_the_machines(self):
        at_most = self._announce()[0].resources
        self.assertEqual((at_most.cpu_quota, at_most.cpu_period), (800000, 100000))
        self.assertEqual(at_most.mem_limit, 16 * GIB)
        self.assertEqual(at_most.disk_space, 500 * GIB)

    def test_host_limits_cap_them(self):
        caps = host_limits.Ceilings(cores=2.5, ram_bytes=4 * GIB, disk_bytes=None)
        at_most = self._announce(caps=caps)[0].resources
        self.assertEqual(at_most.cpu_quota, 250000)
        self.assertEqual(at_most.mem_limit, 4 * GIB)
        self.assertEqual(at_most.disk_space, 500 * GIB)

    def test_an_unknown_total_is_left_unset_not_zero(self):
        at_most = self._announce(totals=(None, None, None))[0].resources
        for field in ("cpu_quota", "cpu_period", "mem_limit", "disk_space"):
            self.assertFalse(at_most.HasField(field), field)

    def test_only_measured_scores_are_announced_per_architecture(self):
        entries = self._announce(served=(AMD64, ARM64), scores={
            "linux/amd64": _scores(int_ops_per_sec=900000, mem_bandwidth_1gib_bytes_per_sec=7 * 10 ** 9),
            "linux/arm64": _scores(int_ops_per_sec=20000),
        })
        self.assertEqual(keyvalue.to_dict(entries[0].resources.benchmark), {
            "int_ops_per_sec": 900000,
            "mem_bandwidth_1gib_bytes_per_sec": 7 * 10 ** 9,
        })
        self.assertEqual(keyvalue.to_dict(entries[1].resources.benchmark), {"int_ops_per_sec": 20000})

    def test_the_same_machine_announces_the_same_bytes(self):
        # Ceilings, not headroom: the signed-announcement cache keys on the content
        # digest, and an announcement that moved with load would never hit it.
        self.assertEqual(
            [e.SerializeToString() for e in self._announce()],
            [e.SerializeToString() for e in self._announce()],
        )


def _announced(arch_tags=AMD64, **at_most):
    entry = celaut.ArchitectureResources()
    entry.architecture.tags.extend(arch_tags)
    scores = at_most.pop("benchmark", {})
    for field, value in at_most.items():
        setattr(entry.resources, field, value)
    keyvalue.update(entry.resources.benchmark, scores)
    return entry


def _request(benchmark_required=None, **limits):
    """What is asked about: one Sysresources, its benchmark the required minimum."""
    ask = celaut.Sysresources(**limits)
    if benchmark_required:
        keyvalue.update(ask.benchmark, benchmark_required)
    return ask


class RequestMisfitTests(unittest.TestCase):

    def test_a_peer_that_announced_nothing_is_asked(self):
        self.assertIsNone(ar.request_misfit([], "linux/amd64", _request(mem_limit=10 ** 15)))

    def test_an_unknown_request_architecture_is_asked(self):
        self.assertIsNone(ar.request_misfit([_announced()], None, _request(mem_limit=10 ** 15)))

    def test_an_architecture_it_does_not_announce_is_skipped(self):
        reason = ar.request_misfit([_announced(ARM64)], "linux/amd64", _request())
        self.assertIn("does not run linux/amd64", reason)
        self.assertIn("linux/arm64", reason)

    def test_an_alias_in_the_announcement_still_matches(self):
        self.assertIsNone(ar.request_misfit([_announced(["x86_64"])], "linux/amd64", _request()))

    def test_a_limit_above_the_announced_ceiling_is_skipped(self):
        announced = [_announced(mem_limit=GIB, disk_space=10 * GIB, cpu_quota=200000, cpu_period=100000)]
        for request, word in (
                (_request(mem_limit=2 * GIB), "mem_limit"),
                (_request(disk_space=20 * GIB), "disk_space"),
                (_request(cpu_quota=400000, cpu_period=100000), "cores"),
        ):
            with self.subTest(word=word):
                self.assertIn(word, ar.request_misfit(announced, "linux/amd64", request))

    def test_a_request_within_the_ceilings_is_asked(self):
        announced = [_announced(mem_limit=GIB, disk_space=10 * GIB, cpu_quota=200000, cpu_period=100000)]
        self.assertIsNone(ar.request_misfit(
            announced, "linux/amd64", _request(mem_limit=GIB, disk_space=GIB, cpu_quota=100000, cpu_period=100000)
        ))

    def test_a_ceiling_it_did_not_announce_is_no_reason_to_skip(self):
        self.assertIsNone(ar.request_misfit([_announced()], "linux/amd64", _request(mem_limit=10 ** 15)))

    def test_a_required_benchmark_above_the_announced_score_is_skipped(self):
        announced = [_announced(benchmark={"int_ops_per_sec": 20000})]
        reason = ar.request_misfit(
            announced, "linux/amd64", _request(benchmark_required={"int_ops_per_sec": 500000})
        )
        self.assertIn("int_ops_per_sec", reason)
        self.assertIn("by the peer for linux/amd64: 20000", reason)

    def test_a_primitive_the_peer_did_not_measure_is_asked(self):
        announced = [_announced(benchmark={"int_ops_per_sec": 20000})]
        self.assertIsNone(ar.request_misfit(
            announced, "linux/amd64", _request(benchmark_required={"flt_ops_per_sec": 10 ** 12})
        ))

    def test_bandwidth_is_judged_over_the_working_set_it_names_or_the_next_larger(self):
        slow_256 = [_announced(benchmark={"mem_bandwidth_256mib_bytes_per_sec": 10 ** 8})]
        asked = _request(benchmark_required={"mem_bandwidth_256mib_bytes_per_sec": 10 ** 9})
        self.assertIn("mem_bandwidth_256mib_bytes_per_sec", ar.request_misfit(slow_256, "linux/amd64", asked))
        # Announced over a larger working set only: that score is a safe floor, so a
        # fast one fits and a slow one rules the peer out.
        fast_1gib = [_announced(benchmark={"mem_bandwidth_1gib_bytes_per_sec": 10 ** 12})]
        slow_1gib = [_announced(benchmark={"mem_bandwidth_1gib_bytes_per_sec": 10 ** 8})]
        self.assertIsNone(ar.request_misfit(fast_1gib, "linux/amd64", asked))
        self.assertIn("mem_bandwidth_1gib_bytes_per_sec", ar.request_misfit(slow_1gib, "linux/amd64", asked))
        # Announced over a smaller one only: says nothing about this, so the peer is asked.
        small = [_announced(benchmark={"mem_bandwidth_64mib_bytes_per_sec": 1})]
        self.assertIsNone(ar.request_misfit(small, "linux/amd64", asked))

    def test_the_emulated_architecture_is_judged_by_its_own_scores(self):
        # The point of #448: a peer fast natively and slow under TCG must be skipped
        # for the emulated architecture only.
        announced = [
            _announced(AMD64, benchmark={"int_ops_per_sec": 900000}),
            _announced(ARM64, benchmark={"int_ops_per_sec": 20000}),
        ]
        required = _request(benchmark_required={"int_ops_per_sec": 500000})
        self.assertIsNone(ar.request_misfit(announced, "linux/amd64", required))
        self.assertIsNotNone(ar.request_misfit(announced, "linux/arm64", required))


class StoredAnnouncementTests(unittest.TestCase):

    def _stored(self, blob):
        with patch("src.database.sql_connection.SQLConnection.get_peer_advertisement", return_value=blob):
            return ar.stored_announcement("peer-a")

    def test_the_stored_peers_resources_are_read_back(self):
        peer = celaut.Peer()
        peer.resources.append(_announced(mem_limit=GIB))
        self.assertEqual(self._stored(peer.SerializeToString()), [_announced(mem_limit=GIB)])

    def test_nothing_stored_or_unreadable_is_nothing_announced(self):
        self.assertEqual(self._stored(None), [])
        self.assertEqual(self._stored(b"\xff\xff\xff"), [])

    def test_an_announcement_stored_before_the_field_existed_announces_nothing(self):
        # Old Peer: ts as an int64 varint at field 8, where resources now is. Never a
        # crash, and the peer is called as before.
        old_ts_at_field_8 = bytes([8 << 3 | 0]) + bytes([0xE8, 0x07])
        self.assertEqual(self._stored(old_ts_at_field_8), [])


class ShouldSkipPeerTests(unittest.TestCase):

    def test_it_logs_why(self):
        with patch.object(ar, "stored_announcement", return_value=[_announced(ARM64)]), \
                patch.object(ar.log, "LOGGER") as logger:
            self.assertTrue(ar.should_skip_peer("peer-a", "linux/amd64", _request(), "GetServiceEstimatedCost"))
        self.assertIn("Skipping GetServiceEstimatedCost on peer peer-a", logger.call_args.args[0])

    def test_a_peer_that_announced_nothing_is_not_skipped(self):
        with patch.object(ar, "stored_announcement", return_value=[]):
            self.assertFalse(ar.should_skip_peer("peer-a", "linux/amd64", _request(), "x"))


class BalancerPreFilterTests(unittest.TestCase):
    """GetServiceEstimatedCost is not asked of a peer that ruled itself out."""

    def test_only_the_peers_that_could_fit_are_asked_for_a_price(self):
        from src.balancers.execution_balancer import execution_balancer as eb

        announcements = {
            "arm-only": [_announced(ARM64)],
            "too-small": [_announced(AMD64, mem_limit=GIB)],
            "silent": [],
            "fits": [_announced(AMD64, mem_limit=64 * GIB)],
        }
        service = celaut.Service()
        service.container.architecture.tags.append("linux/amd64")
        service.container.resources.at_most.mem_limit = 2 * GIB
        asked = []

        def _env(key, default=None):
            return {"network.EXECUTE_LOCALLY": False, "network.DELEGATE_EXECUTION": True}.get(key, default)

        with patch.object(eb, "peers_id_iterator", return_value=iter(announcements)), \
                patch.object(ar, "stored_announcement", side_effect=lambda pid: announcements[pid]), \
                patch.object(eb, "estimate_cost_on_peer", side_effect=lambda peer_id, **_: asked.append(peer_id)), \
                patch.object(eb.env_manager, "get", side_effect=_env), \
                patch.object(eb, "estimated_cost_sorter", return_value=iter([])):
            list(eb.execution_balancer(
                service_id="svc", resources=service.container.resources,
                metadata=celaut.Metadata(), configuration=celaut.Configuration(),
                arch=None, service=service,
            ))
        self.assertEqual(asked, ["silent", "fits"])


class WorkloadPreFilterTests(unittest.TestCase):
    """GetResourceAvailability is not asked of a peer that ruled itself out."""

    def test_only_the_peers_that_could_fit_are_asked(self):
        from src.utils.cost_functions import workload_admission as wa

        announcements = {"arm-only": [_announced(ARM64)], "fits": [_announced(AMD64)]}
        request = celaut.ArchitectureResources()
        request.architecture.tags.append("linux/amd64")
        asked = []

        with patch.object(wa, "_local_resource_availability", return_value={"can_execute": False}), \
                patch("src.utils.utils.peers_id_iterator", side_effect=lambda **_: iter(announcements)), \
                patch.object(ar, "stored_announcement", side_effect=lambda pid: announcements[pid]), \
                patch.object(wa, "check_resource_availability_on_peer",
                             side_effect=lambda pid, req: asked.append(pid) or True), \
                patch.object(wa.env_manager, "get", side_effect=lambda key, default=None: default):
            self.assertTrue(wa._workload_group_is_satisfiable(request, None))
        self.assertEqual(asked, ["fits"])


class SignatureCoversResourcesTests(unittest.TestCase):
    """A relay cannot rewrite what a peer announced it supports."""

    @classmethod
    def setUpClass(cls):
        from tests.test_peer_identity_registration import _PeerFixture

        cls.Fixture = _PeerFixture

    def setUp(self):
        self.fixture = self.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def _peer(self, ts=100, **at_most):
        def prepare(peer):
            peer.resources.append(_announced(AMD64, **at_most))
        return self.fixture._peer([("10.0.0.1", 9999)], ts=ts, prepare=prepare)

    def test_a_signed_announcement_with_resources_verifies(self):
        import src.manager.manager as manager

        peer = self._peer(mem_limit=GIB, benchmark={"int_ops_per_sec": 1})
        self.assertEqual(manager.verified_peer_public_key(peer), self.fixture.pubkey)

    def test_raising_a_score_or_dropping_an_architecture_breaks_it(self):
        import src.manager.manager as manager

        for tamper in (
                lambda p: keyvalue.set_value(p.resources[0].resources.benchmark, "int_ops_per_sec", 10 ** 9),
                lambda p: p.ClearField("resources"),
                lambda p: p.resources.append(_announced(ARM64)),
                lambda p: setattr(p.resources[0].resources, "mem_limit", 10 ** 15),
        ):
            with self.subTest(tamper=tamper):
                peer = self._peer(mem_limit=GIB, benchmark={"int_ops_per_sec": 1})
                tamper(peer)
                self.assertIsNone(manager.verified_peer_public_key(peer))

    def test_the_last_announcement_is_the_one_stored_and_read_back(self):
        import src.manager.manager as manager

        manager.add_peer_instance(self._peer(ts=100, mem_limit=GIB))
        manager.add_peer_instance(self._peer(ts=200, mem_limit=4 * GIB))
        self.assertEqual(
            ar.stored_announcement(self.fixture.pubkey)[0].resources.mem_limit, 4 * GIB
        )
        # A replayed older one does not overwrite it.
        manager.add_peer_instance(self._peer(ts=150, mem_limit=GIB))
        self.assertEqual(
            ar.stored_announcement(self.fixture.pubkey)[0].resources.mem_limit, 4 * GIB
        )


if __name__ == "__main__":
    unittest.main()

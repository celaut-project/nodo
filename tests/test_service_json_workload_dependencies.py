import unittest

from protos import celaut_pb2, pack_pb2
from src.packers.service_json import parse_service_spec, populate_environment_workloads
from src.utils.hashing import SHA3_256_ID


def _clause(*groups):
    return {"any_of": [{"workloads": list(group)} for group in groups]}


def _resources(at_init=None, at_most=None):
    resources = {}
    if at_init is not None:
        resources["at_init"] = at_init
    if at_most is not None:
        resources["at_most"] = at_most
    return {"service": {"container": {"resources": resources}}}


class ServiceJsonWorkloadDependencyTests(unittest.TestCase):
    def test_packs_all_optional_dependency_forms(self):
        service = pack_pb2.Service()
        digest_a = "11" * 32
        digest_b = "22" * 32

        populate_environment_workloads(
            service.possible_environment_workload,
            [
                _clause([
                    {"count": 1, "dependency": _resources(at_most={"mem_limit": 100})},
                    {
                        "count": 2,
                        "dependency": {
                            "hash": [digest_a],
                            "on_filesystem": True,
                        },
                    },
                    {
                        "count": 3,
                        "dependency": {
                            "service": {
                                "prose": "Scheduler projection only",
                                "container": {
                                    "resources": {
                                        "at_init": {"mem_limit": 400},
                                        "at_most": {"mem_limit": 800},
                                    }
                                },
                                "possible_environment_workload": [
                                    _clause([
                                        {
                                            "count": 4,
                                            "dependency": _resources(
                                                at_most={"disk_space": 900}
                                            ),
                                        }
                                    ])
                                ],
                                "initial_environment_workload": [
                                    _clause([{"count": 5, "dependency": None}])
                                ],
                            }
                        },
                    },
                    {
                        "count": 1,
                        "dependency": {
                            "hash": [
                                {"type": "sha3_256", "value": digest_b}
                            ],
                            "service": {
                                "prose": "Complete embedded dependency",
                                "container": {
                                    "architecture": {"tags": ["linux/amd64"]},
                                    "resources": {
                                        "at_init": {"mem_limit": 1024},
                                        "at_most": {"mem_limit": 2048},
                                    },
                                },
                            },
                            "is_completed": True,
                            "on_filesystem": False,
                        },
                    },
                ])
            ],
        )

        canonical = celaut_pb2.Service.FromString(service.SerializeToString())
        workloads = canonical.possible_environment_workload[0].any_of[0].workloads

        resources_only = workloads[0].dependency
        self.assertFalse(resources_only.hash)
        self.assertEqual(resources_only.service.container.resources.at_most.mem_limit, 100)

        hash_only = workloads[1].dependency
        self.assertEqual(hash_only.hash[0].type, SHA3_256_ID)
        self.assertEqual(hash_only.hash[0].value, bytes.fromhex(digest_a))
        self.assertTrue(hash_only.on_filesystem)
        self.assertFalse(hash_only.is_completed)
        self.assertFalse(hash_only.HasField("service"))

        partial = workloads[2].dependency
        self.assertTrue(partial.HasField("service"))
        self.assertFalse(partial.is_completed)
        self.assertFalse(partial.on_filesystem)
        self.assertEqual(partial.service.container.resources.at_init.mem_limit, 400)
        nested = partial.service.possible_environment_workload[0].any_of[0].workloads[0]
        self.assertEqual(nested.count, 4)
        self.assertEqual(nested.dependency.service.container.resources.at_most.disk_space, 900)
        nested_initial = partial.service.initial_environment_workload[0].any_of[0].workloads[0]
        self.assertEqual(nested_initial.count, 5)
        self.assertFalse(nested_initial.HasField("dependency"))

        complete = workloads[3].dependency
        self.assertTrue(complete.is_completed)
        self.assertFalse(complete.on_filesystem)
        self.assertEqual(complete.hash[0].value, bytes.fromhex(digest_b))
        self.assertEqual(complete.service.prose, "Complete embedded dependency")
        self.assertEqual(
            complete.service.container.architecture.tags[0], "linux/amd64"
        )

    def test_clauses_and_groups_keep_their_order(self):
        """`(G1 or G2) and G3`, the example of #538, in the initial field."""
        service = celaut_pb2.Service()
        g1 = [
            {"count": 1, "dependency": _resources(at_most={"mem_limit": 3 << 30})},
            {"count": 1, "dependency": _resources(at_most={"mem_limit": 4 << 30})},
        ]
        g2 = [{"count": 2, "dependency": _resources(
            at_most={"mem_limit": 1 << 30, "cpu_period": 100000, "cpu_quota": 400000})}]
        g3 = [{"count": 4, "dependency": _resources(
            at_most={"mem_limit": 2 << 30, "cpu_period": 100000, "cpu_quota": 200000})}]

        populate_environment_workloads(
            service.initial_environment_workload, [_clause(g1, g2), _clause(g3)],
            "initial_environment_workload",
        )

        clauses = service.initial_environment_workload
        self.assertEqual(len(clauses), 2)
        self.assertEqual([len(g.workloads) for g in clauses[0].any_of], [2, 1])
        self.assertEqual(clauses[0].any_of[1].workloads[0].count, 2)
        self.assertEqual(
            clauses[1].any_of[0].workloads[0].dependency.service.container.resources
            .at_most.cpu_quota, 200000,
        )
        self.assertFalse(service.possible_environment_workload)

    def test_parse_service_spec_reads_both_fields_and_camel_case(self):
        service = parse_service_spec({
            "prose": "child",
            "initialEnvironmentWorkload": [_clause([{"count": 1, "dependency": {"hash": ["33" * 32]}}])],
            "possible_environment_workload": [_clause([{"count": 2, "dependency": {"hash": ["44" * 32]}}])],
        })
        self.assertEqual(service.initial_environment_workload[0].any_of[0].workloads[0].count, 1)
        self.assertEqual(service.possible_environment_workload[0].any_of[0].workloads[0].count, 2)

    def test_dependency_null_omits_message(self):
        service = celaut_pb2.Service()
        populate_environment_workloads(
            service.possible_environment_workload,
            [_clause([{"dependency": None}])],
        )
        self.assertFalse(
            service.possible_environment_workload[0].any_of[0].workloads[0].HasField(
                "dependency"
            )
        )

    def test_rejects_the_old_shape(self):
        cases = [
            # The scenario list of before #538: a clause with workloads.
            ([{"workloads": [{"count": 1}]}], "A clause is"),
            # Workload.resources is removed.
            ([_clause([{"count": 1, "resources": {"mem_limit": 1}}])],
             "dependency.service.container.resources"),
            ([{"any_of": []}], "at least one group"),
            ([{"any_of": [{"workloads": [], "count": 1}]}], "unknown field"),
            ([{"any_of": "x"}], "must be an array"),
        ]
        for declaration, expected in cases:
            with self.subTest(declaration=declaration):
                with self.assertRaisesRegex(ValueError, expected):
                    populate_environment_workloads(
                        celaut_pb2.Service().possible_environment_workload, declaration,
                    )

    def test_rejects_invalid_dependency_states(self):
        cases = [
            ({"hash": ["not-hex"]}, "valid hexadecimal"),
            ({"hash": ["11" * 32], "on_filesystem": 1}, "must be a boolean"),
            ({"hash": ["11" * 32], "is_stored": True}, "unknown field"),
            (
                {"hash": ["11" * 32], "is_completed": True},
                "requires an embedded service",
            ),
            ({"on_filesystem": True}, "at least one hash or an embedded service"),
        ]

        for dependency, expected in cases:
            with self.subTest(dependency=dependency):
                service = celaut_pb2.Service()
                with self.assertRaisesRegex(ValueError, expected):
                    populate_environment_workloads(
                        service.possible_environment_workload,
                        [_clause([{"dependency": dependency}])],
                    )


if __name__ == "__main__":
    unittest.main()

import unittest

from protos import celaut_pb2, pack_pb2
from src.packers.service_json import populate_possible_environment_workloads
from src.utils.hashing import SHA3_256_ID


class ServiceJsonWorkloadDependencyTests(unittest.TestCase):
    def test_packs_all_optional_dependency_forms(self):
        service = pack_pb2.Service()
        digest_a = "11" * 32
        digest_b = "22" * 32

        populate_possible_environment_workloads(
            service,
            [
                {
                    "workloads": [
                        {"count": 1, "resources": {"mem_limit": 100}},
                        {
                            "count": 2,
                            "resources": {"mem_limit": 200},
                            "dependency": {
                                "hash": [digest_a],
                                "on_filesystem": True,
                            },
                        },
                        {
                            "count": 3,
                            "resources": {"mem_limit": 300},
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
                                        {
                                            "workloads": [
                                                {
                                                    "count": 4,
                                                    "resources": {"disk_space": 900},
                                                    "dependency": None,
                                                }
                                            ]
                                        }
                                    ],
                                }
                            },
                        },
                        {
                            "count": 1,
                            "resources": {},
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
                    ]
                }
            ],
        )

        canonical = celaut_pb2.Service.FromString(service.SerializeToString())
        workloads = canonical.possible_environment_workload[0].workloads

        self.assertFalse(workloads[0].HasField("dependency"))

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
        nested = partial.service.possible_environment_workload[0].workloads[0]
        self.assertEqual(nested.count, 4)
        self.assertEqual(nested.resources.disk_space, 900)
        self.assertFalse(nested.HasField("dependency"))

        complete = workloads[3].dependency
        self.assertTrue(complete.is_completed)
        self.assertFalse(complete.on_filesystem)
        self.assertEqual(complete.hash[0].value, bytes.fromhex(digest_b))
        self.assertEqual(complete.service.prose, "Complete embedded dependency")
        self.assertEqual(
            complete.service.container.architecture.tags[0], "linux/amd64"
        )

    def test_dependency_null_omits_message(self):
        service = celaut_pb2.Service()
        populate_possible_environment_workloads(
            service,
            [{"workloads": [{"dependency": None}]}],
        )
        self.assertFalse(
            service.possible_environment_workload[0].workloads[0].HasField(
                "dependency"
            )
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
                    populate_possible_environment_workloads(
                        service,
                        [{"workloads": [{"dependency": dependency}]}],
                    )


if __name__ == "__main__":
    unittest.main()

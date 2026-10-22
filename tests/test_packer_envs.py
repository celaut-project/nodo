"""`service.json`'s `envs` -> `Service.Container.environment_variables`.

`parseContainer` needs BuildKit through the packer's constructor, so the part that
turns `envs` into the declaration is exercised through `pack.Service` and the
`celaut.Service` re-read that `save` does.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut, pack_pb2
    from src.utils import keyvalue
    from src.utils.service_envs import env_specs
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


@unittest.skipIf(IMPORT_ERROR is not None, f"import failed: {IMPORT_ERROR}")
class TestPackedEnvs(unittest.TestCase):

    def test_declared_names_survive_the_celaut_reread(self):
        packed = pack_pb2.Service()
        keyvalue.from_dict(
            packed.container.environment_variables,
            {"GITHUB_TOKEN": celaut.DataFormat(), "CLAUDE_CODE_OAUTH_TOKEN": celaut.DataFormat()},
        )
        spec = celaut.Service()
        spec.ParseFromString(packed.SerializeToString())
        self.assertEqual(
            [s.name for s in env_specs(spec)], ["CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN"]
        )

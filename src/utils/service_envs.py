"""Which environment variables a service asks for, and which a launch leaves out.

A service asks only for the names in ``Service.Container.environment_variables``,
each with its ``DataFormat`` (tags, prose).

A ``${NAME}`` placeholder in a ``Service.Network.formal`` (issue #385) is not a
request for the user/parent: it marks a key as open. The node does not resolve that
network until it has a value, and the instance may fill it later through
``Gateway.ResolveNetwork``. A value given with ``-e`` still replaces the
placeholder at launch. To make the user/parent give one, the author declares the
variable in the container too.

Nothing says which variables are mandatory, and the node does not guess: it asks
for every declared one, and the caller decides what to leave empty.
"""
from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple

from protos import celaut_pb2 as celaut
from src.utils import keyvalue


@dataclass(frozen=True)
class EnvSpec:
    name: str
    tags: Tuple[str, ...] = ()
    prose: str = ""

    def to_json(self) -> Dict[str, object]:
        return {"name": self.name, "tags": list(self.tags), "prose": self.prose}


def env_specs(service: celaut.Service) -> List[EnvSpec]:
    """Every variable ``service`` declares in its container, in order."""
    return [
        EnvSpec(name=name, tags=tuple(data_format.tags), prose=data_format.prose)
        for name, data_format in keyvalue.items(service.container.environment_variables)
    ]


def missing_envs(specs: List[EnvSpec], envs: Mapping[str, str]) -> List[EnvSpec]:
    """The specs that ``envs`` gives no value for, in the order of ``specs``."""
    return [spec for spec in specs if spec.name not in envs]

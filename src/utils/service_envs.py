"""Which environment variables a service asks for, and which a launch leaves out.

A service names its variables in two places:

* ``Service.Container.environment_variables`` -- each declared name with its
  ``DataFormat`` (tags, prose).
* ``${NAME}`` placeholders in a ``Service.Network.formal`` (issue #385).

Neither says which are mandatory, and the node does not guess: it asks for every
one, and the caller decides what to leave empty. A service may not work without
all of them. A placeholder variable that the container does not declare (the proto
says senders do not set ``environment_variables`` yet) is still listed, so the
caller is asked for it.
"""
from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple

from protos import celaut_pb2 as celaut
from src.manager.network_templates import find_placeholders
from src.utils import keyvalue


@dataclass(frozen=True)
class EnvSpec:
    name: str
    tags: Tuple[str, ...] = ()
    prose: str = ""

    def to_json(self) -> Dict[str, object]:
        return {"name": self.name, "tags": list(self.tags), "prose": self.prose}


def env_specs(service: celaut.Service) -> List[EnvSpec]:
    """Every variable ``service`` asks for: the declared ones in order, then the undeclared placeholders."""
    specs: List[EnvSpec] = []
    seen = set()
    for name, data_format in keyvalue.items(service.container.environment_variables):
        seen.add(name)
        specs.append(EnvSpec(name=name, tags=tuple(data_format.tags), prose=data_format.prose))
    for network in service.network:
        for name in find_placeholders(network.formal).values():
            if name not in seen:
                seen.add(name)
                specs.append(EnvSpec(name=name))
    return specs


def missing_envs(specs: List[EnvSpec], envs: Mapping[str, str]) -> List[EnvSpec]:
    """The specs that ``envs`` gives no value for, in the order of ``specs``."""
    return [spec for spec in specs if spec.name not in envs]

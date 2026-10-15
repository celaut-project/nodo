"""Which environment variables a service asks for, and which a launch leaves out.

A service names its variables in two places, and neither says "required":

* ``Service.Container.environment_variables`` -- each declared name with its
  ``DataFormat`` (tags, prose). Nothing in the proto marks one as mandatory, so a
  declared variable is **optional**: left out, the instance starts without it.
* ``${NAME}`` placeholders in a ``Service.Network.formal`` (issue #385). Left out,
  the node does not resolve that network at launch. ``nodo execute`` treats these
  as **required**: a service that templates its network on a variable cannot reach
  the peers it was written for until somebody answers it.

A placeholder variable that the container does not declare (the proto says senders
do not set ``environment_variables`` yet) is still listed, as required, so the
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
    # The networks (each named by its tags) that template a key on this variable.
    # Not empty exactly when the variable is required.
    networks: Tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        return bool(self.networks)

    def to_json(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "tags": list(self.tags),
            "prose": self.prose,
            "required": self.required,
            "networks": list(self.networks),
        }


def _network_label(network: celaut.Service.Network) -> str:
    return ", ".join(network.tags) or "(untagged network)"


def env_specs(service: celaut.Service) -> List[EnvSpec]:
    """Every variable ``service`` asks for: required ones first, then by declaration order."""
    networks: Dict[str, List[str]] = {}
    for network in service.network:
        for variable in find_placeholders(network.formal).values():
            labels = networks.setdefault(variable, [])
            label = _network_label(network)
            if label not in labels:
                labels.append(label)

    specs: List[EnvSpec] = []
    for name, data_format in keyvalue.items(service.container.environment_variables):
        specs.append(EnvSpec(
            name=name,
            tags=tuple(data_format.tags),
            prose=data_format.prose,
            networks=tuple(networks.pop(name, ())),
        ))
    for name, labels in networks.items():
        specs.append(EnvSpec(name=name, networks=tuple(labels)))

    # `sorted` is stable, so each group keeps its declaration order.
    return sorted(specs, key=lambda spec: not spec.required)


def is_answered(spec: EnvSpec, envs: Mapping[str, str]) -> bool:
    """Whether ``envs`` gives ``spec`` a value the node can use.

    An optional variable only has to be present. A required one fills a network
    ``formal`` value, which must be one line of text: an empty value or one with a
    line break leaves the network unresolved (``network_templates.substitute``).
    """
    if spec.name not in envs:
        return False
    if not spec.required:
        return True
    value = envs[spec.name]
    return bool(value) and "\n" not in value and "\r" not in value


def missing_envs(specs: List[EnvSpec], envs: Mapping[str, str]) -> List[EnvSpec]:
    """The specs that ``envs`` does not answer, in the order of ``specs``."""
    return [spec for spec in specs if not is_answered(spec, envs)]

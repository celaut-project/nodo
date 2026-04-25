import re
from typing import Optional, Sequence

from protos import celaut_pb2


INTERNAL_INSTANCE_NAME_ENV = "__nodo_instance_name"
_INSTANCE_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_INSTANCE_NAME_LENGTH = 63

_ADJECTIVES: Sequence[str] = (
    "agile",
    "brisk",
    "calm",
    "clever",
    "daring",
    "eager",
    "fierce",
    "gentle",
    "jolly",
    "keen",
    "lively",
    "mellow",
    "noble",
    "proud",
    "quick",
    "royal",
    "steady",
    "tidy",
    "vivid",
    "witty",
)

_NOUNS: Sequence[str] = (
    "anchor",
    "badger",
    "comet",
    "delta",
    "ember",
    "falcon",
    "grove",
    "harbor",
    "island",
    "jungle",
    "meadow",
    "nova",
    "orbit",
    "panda",
    "quartz",
    "rocket",
    "summit",
    "thunder",
    "vector",
    "willow",
)


def normalize_instance_name(name: str) -> str:
    normalized = str(name or "").strip().lower().replace("_", "-").replace(" ", "-")
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        raise ValueError("Instance name cannot be empty.")
    if len(normalized) > _MAX_INSTANCE_NAME_LENGTH:
        raise ValueError(
            f"Instance name is too long ({len(normalized)}). Max length is {_MAX_INSTANCE_NAME_LENGTH}."
        )
    if not _INSTANCE_NAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Invalid instance name. Use lowercase letters, numbers, or single hyphens between segments."
        )
    return normalized


def random_instance_name(randbelow) -> str:
    adjective = _ADJECTIVES[randbelow(len(_ADJECTIVES))]
    noun = _NOUNS[randbelow(len(_NOUNS))]
    return f"{adjective}-{noun}"


def inject_instance_name(
    config: celaut_pb2.Configuration,
    instance_name: Optional[str],
) -> celaut_pb2.Configuration:
    if not instance_name:
        return config
    config.environment_variables[INTERNAL_INSTANCE_NAME_ENV] = normalize_instance_name(instance_name).encode("utf-8")
    return config


def extract_instance_name(
    config: Optional[celaut_pb2.Configuration],
) -> tuple[Optional[str], Optional[celaut_pb2.Configuration]]:
    if config is None:
        return None, None

    sanitized = celaut_pb2.Configuration()
    sanitized.CopyFrom(config)
    raw_value = sanitized.environment_variables.get(INTERNAL_INSTANCE_NAME_ENV, b"")
    if INTERNAL_INSTANCE_NAME_ENV in sanitized.environment_variables:
        del sanitized.environment_variables[INTERNAL_INSTANCE_NAME_ENV]

    instance_name = None
    if raw_value:
        instance_name = normalize_instance_name(raw_value.decode("utf-8"))
    return instance_name, sanitized

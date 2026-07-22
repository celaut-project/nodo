"""Parsing helpers for protobuf-backed fields in ``service.json``."""

from copy import deepcopy
from typing import Any, Mapping, Sequence

from google.protobuf import json_format

from protos import celaut_pb2 as celaut
from src.utils.hashing import resolve_hash_config


def _path(parent: str, child: str) -> str:
    return f"{parent}.{child}" if parent else child


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"service.json {path} must be an object.")
    return value


def _require_list(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValueError(f"service.json {path} must be an array.")
    return value


def _parse_hex(value: Any, path: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"service.json {path} must be a hexadecimal string.")
    normalized = value.strip().lower()
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    try:
        return bytes.fromhex(normalized)
    except ValueError as exc:
        raise ValueError(
            f"service.json {path} must contain valid hexadecimal bytes."
        ) from exc


def _parse_hash(value: Any, path: str) -> celaut.Metadata.HashTag.Hash:
    if isinstance(value, str):
        hash_type = "sha3_256"
        hash_value = value
    else:
        item = _require_mapping(value, path)
        hash_type = item.get("type", "sha3_256")
        hash_value = item.get("value")
        if hash_value is None:
            raise ValueError(f"service.json {path}.value is required.")

    try:
        hash_spec = resolve_hash_config(str(hash_type))
    except ValueError as exc:
        raise ValueError(f"service.json {path}.type: {exc}") from exc

    return celaut.Metadata.HashTag.Hash(
        type=hash_spec.id_bytes,
        value=_parse_hex(hash_value, _path(path, "value")),
    )


def _pop_alias(document: dict, snake_case: str, camel_case: str, path: str):
    has_snake = snake_case in document
    has_camel = camel_case in document
    if has_snake and has_camel:
        raise ValueError(
            f"service.json {path} cannot define both '{snake_case}' and '{camel_case}'."
        )
    if has_snake:
        return document.pop(snake_case)
    if has_camel:
        return document.pop(camel_case)
    return []


def parse_service_spec(value: Any, path: str = "service") -> celaut.Service:
    """Parse a protobuf-JSON Service, with recursive workload dependencies.

    The nested service follows the protobuf JSON shape (``container.resources``,
    not the main packer's top-level ``resources`` convenience field). Hashes in
    workload dependencies are removed first and parsed as ergonomic hex values.
    Other protobuf ``bytes`` fields keep protobuf JSON's standard base64 format.
    """

    source = _require_mapping(value, path)
    document = deepcopy(dict(source))
    workloads = _pop_alias(
        document,
        "possible_environment_workload",
        "possibleEnvironmentWorkload",
        path,
    )
    service = celaut.Service()
    try:
        json_format.ParseDict(document, service, ignore_unknown_fields=False)
    except (json_format.ParseError, TypeError, ValueError) as exc:
        raise ValueError(f"service.json {path} is not a valid Service: {exc}") from exc

    populate_possible_environment_workloads(
        service,
        workloads,
        _path(path, "possible_environment_workload"),
    )
    return service


def _populate_dependency(target, value: Any, path: str) -> None:
    source = _require_mapping(value, path)
    hashes = source.get("hash", [])
    if not isinstance(hashes, list):
        raise ValueError(f"service.json {path}.hash must be an array.")

    supported = {"hash", "service", "is_completed", "on_filesystem"}
    unknown = sorted(set(source) - supported)
    if unknown:
        raise ValueError(
            f"service.json {path} contains unknown field(s): {', '.join(unknown)}."
        )

    for index, item in enumerate(hashes):
        target.hash.append(_parse_hash(item, f"{path}.hash[{index}]"))

    nested_service = source.get("service")
    if nested_service is not None:
        target.service.CopyFrom(parse_service_spec(nested_service, _path(path, "service")))

    for field in ("is_completed", "on_filesystem"):
        if field not in source:
            continue
        if not isinstance(source[field], bool):
            raise ValueError(f"service.json {path}.{field} must be a boolean.")
        setattr(target, field, source[field])

    if target.is_completed and not target.HasField("service"):
        raise ValueError(
            f"service.json {path}.is_completed=true requires an embedded service."
        )
    if not target.hash and not target.HasField("service"):
        raise ValueError(
            f"service.json {path} must contain at least one hash or an embedded service."
        )


def populate_possible_environment_workloads(
    service: celaut.Service,
    value: Any,
    path: str = "possible_environment_workload",
) -> None:
    """Append service.json workload scenarios to ``service``."""

    scenarios = _require_list(value, path)
    for scenario_index, scenario_value in enumerate(scenarios):
        scenario_path = f"{path}[{scenario_index}]"
        scenario_source = _require_mapping(scenario_value, scenario_path)
        unknown_scenario = sorted(set(scenario_source) - {"workloads"})
        if unknown_scenario:
            raise ValueError(
                f"service.json {scenario_path} contains unknown field(s): "
                f"{', '.join(unknown_scenario)}."
            )
        workloads = _require_list(
            scenario_source.get("workloads", []), _path(scenario_path, "workloads")
        )
        scenario = service.possible_environment_workload.add()

        for workload_index, workload_value in enumerate(workloads):
            workload_path = f"{scenario_path}.workloads[{workload_index}]"
            workload_source = _require_mapping(workload_value, workload_path)
            unknown_workload = sorted(
                set(workload_source) - {"count", "resources", "dependency"}
            )
            if unknown_workload:
                raise ValueError(
                    f"service.json {workload_path} contains unknown field(s): "
                    f"{', '.join(unknown_workload)}."
                )

            workload = scenario.workloads.add()
            count = workload_source.get("count", 1)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(
                    f"service.json {workload_path}.count must be a non-negative integer."
                )
            workload.count = count

            resources = workload_source.get("resources", {})
            try:
                json_format.ParseDict(
                    resources,
                    workload.resources,
                    ignore_unknown_fields=False,
                )
            except (json_format.ParseError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"service.json {workload_path}.resources is not valid: {exc}"
                ) from exc

            dependency = workload_source.get("dependency")
            if dependency is not None:
                _populate_dependency(
                    workload.dependency, dependency, _path(workload_path, "dependency")
                )

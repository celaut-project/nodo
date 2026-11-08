"""Parsing helpers for protobuf-backed fields in ``service.json``."""

from copy import deepcopy
from typing import Any, Mapping, Sequence

from google.protobuf import json_format

from protos import celaut_pb2 as celaut
from src.utils import keyvalue
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


WORKLOAD_FIELDS = (
    ("possible_environment_workload", "possibleEnvironmentWorkload"),
    ("initial_environment_workload", "initialEnvironmentWorkload"),
)


def parse_service_spec(value: Any, path: str = "service") -> celaut.Service:
    """Parse a protobuf-JSON Service, with recursive workload dependencies.

    The nested service follows the protobuf JSON shape (``container.resources``,
    not the main packer's top-level ``resources`` convenience field). Hashes in
    workload dependencies are removed first and parsed as ergonomic hex values.
    Other protobuf ``bytes`` fields keep protobuf JSON's standard base64 format.
    """

    source = _require_mapping(value, path)
    document = deepcopy(dict(source))
    declarations = {
        snake_case: _pop_alias(document, snake_case, camel_case, path)
        for snake_case, camel_case in WORKLOAD_FIELDS
    }
    service = celaut.Service()
    try:
        json_format.ParseDict(
            keyvalue.json_objects_to_entries(document, service.DESCRIPTOR, path),
            service,
            ignore_unknown_fields=False,
        )
    except (json_format.ParseError, TypeError, ValueError) as exc:
        raise ValueError(f"service.json {path} is not a valid Service: {exc}") from exc

    for field, clauses in declarations.items():
        populate_environment_workloads(
            getattr(service, field), clauses, _path(path, field)
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


def _unknown_fields(source: Mapping[str, Any], supported: set, path: str, hint: str = "") -> None:
    unknown = sorted(set(source) - supported)
    if unknown:
        raise ValueError(
            f"service.json {path} contains unknown field(s): {', '.join(unknown)}.{hint}"
        )


def populate_environment_workloads(
    clauses_field,
    value: Any,
    path: str = "possible_environment_workload",
) -> None:
    """Append the service.json clauses of a workload declaration to ``clauses_field``.

    ``clauses_field`` is ``Service.possible_environment_workload`` or
    ``Service.initial_environment_workload``. The declaration is in conjunctive normal
    form: a list of clauses (AND), each clause ``{"any_of": [group, ...]}`` (OR), each
    group ``{"workloads": [workload, ...]}`` (AND, at the same time), and each workload
    ``{"count": n, "dependency": {...}}``. The resources of a workload are in
    ``dependency.service.container.resources``.
    """

    clauses = _require_list(value, path)
    for clause_index, clause_value in enumerate(clauses):
        clause_path = f"{path}[{clause_index}]"
        clause_source = _require_mapping(clause_value, clause_path)
        _unknown_fields(
            clause_source, {"any_of"}, clause_path,
            " A clause is {\"any_of\": [{\"workloads\": [...]}, ...]}."
            if "workloads" in clause_source else "",
        )
        groups = _require_list(
            clause_source.get("any_of", []), _path(clause_path, "any_of")
        )
        if not groups:
            raise ValueError(
                f"service.json {clause_path}.any_of must have at least one group."
            )
        clause = clauses_field.add()

        for group_index, group_value in enumerate(groups):
            group_path = f"{clause_path}.any_of[{group_index}]"
            group_source = _require_mapping(group_value, group_path)
            _unknown_fields(group_source, {"workloads"}, group_path)
            workloads = _require_list(
                group_source.get("workloads", []), _path(group_path, "workloads")
            )
            group = clause.any_of.add()

            for workload_index, workload_value in enumerate(workloads):
                workload_path = f"{group_path}.workloads[{workload_index}]"
                workload_source = _require_mapping(workload_value, workload_path)
                _unknown_fields(
                    workload_source, {"count", "dependency"}, workload_path,
                    " The resources of a workload are in "
                    "dependency.service.container.resources."
                    if "resources" in workload_source else "",
                )

                workload = group.workloads.add()
                count = workload_source.get("count", 1)
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError(
                        f"service.json {workload_path}.count must be a non-negative integer."
                    )
                workload.count = count

                dependency = workload_source.get("dependency")
                if dependency is not None:
                    _populate_dependency(
                        workload.dependency, dependency, _path(workload_path, "dependency")
                    )

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from protos import celaut_pb2
from src.utils.service_content import read_service_content
from src.commands.__by_tag import get_id
from src.utils.config import ConfigManager
from src.utils.hashing import get_configured_hash_spec, hash_stream


@dataclass
class IntegrityIssue:
    service: str
    code: str
    detail: str
    fixed: bool = False


@dataclass
class IntegrityReport:
    hash_name: str
    hash_id: str
    checked: int = 0
    fixed: int = 0
    issues: List[IntegrityIssue] = field(default_factory=list)

    def as_dict(self) -> Dict:
        return {
            "hash_name": self.hash_name,
            "hash_id": self.hash_id,
            "checked": self.checked,
            "fixed": self.fixed,
            "issues": [
                {
                    "service": issue.service,
                    "code": issue.code,
                    "detail": issue.detail,
                    "fixed": issue.fixed,
                }
                for issue in self.issues
            ],
            "ok": len(self.issues) == 0,
        }


def _compute_service_hash(service_path: Path, hash_spec) -> str:
    return hash_stream(read_service_content(service_path), hash_spec).hex()


def _read_metadata(metadata_path: Path) -> Optional[celaut_pb2.Metadata]:
    if not metadata_path.exists():
        return None

    metadata = celaut_pb2.Metadata()
    with metadata_path.open("rb") as metadata_file:
        metadata.ParseFromString(metadata_file.read())
    return metadata


def _metadata_hash_value(metadata: celaut_pb2.Metadata, hash_id: bytes) -> Optional[str]:
    for item in metadata.hashtag.hash:
        if item.type == hash_id:
            return item.value.hex()
    return None


def _upsert_metadata_hash(metadata: celaut_pb2.Metadata, hash_id: bytes, hash_hex: str):
    hash_bytes = bytes.fromhex(hash_hex)
    for item in metadata.hashtag.hash:
        if item.type == hash_id:
            item.value = hash_bytes
            return

    metadata.hashtag.hash.append(
        celaut_pb2.Metadata.HashTag.Hash(
            type=hash_id,
            value=hash_bytes,
        )
    )


def _safe_rename(source: Path, target: Path) -> Optional[str]:
    if source == target:
        return None
    if target.exists():
        return f"Destination already exists: {target}"
    source.rename(target)
    return None


def _resolve_target_service(service: Optional[str], registry_path: Path, metadata_path: Path) -> Optional[str]:
    if not service:
        return None

    service_id = get_id(service) or service
    service_id = service_id.strip()
    if not service_id:
        return None

    if (registry_path / service_id).exists() or (metadata_path / service_id).exists():
        return service_id
    return None


def check_integrity(service: Optional[str] = None, fix: bool = False) -> Dict:
    config = ConfigManager()
    registry_path = Path(str(config.get("REGISTRY")))
    metadata_path = Path(str(config.get("METADATA_REGISTRY")))
    hash_spec = get_configured_hash_spec(config)

    report = IntegrityReport(
        hash_name=hash_spec.name,
        hash_id=hash_spec.id_bytes.hex(),
    )

    resolved_service = _resolve_target_service(service, registry_path, metadata_path)
    if service and not resolved_service:
        report.issues.append(
            IntegrityIssue(
                service=service,
                code="service_not_found",
                detail="Service not found in registry/metadata.",
            )
        )
        return report.as_dict()

    if resolved_service:
        registry_entries = [resolved_service]
    else:
        registry_entries = sorted(
            [entry.name for entry in registry_path.iterdir()]
        ) if registry_path.exists() else []

    for service_name in registry_entries:
        service_entry = registry_path / service_name
        metadata_entry = metadata_path / service_name

        if not service_entry.exists():
            report.issues.append(
                IntegrityIssue(
                    service=service_name,
                    code="missing_service",
                    detail=f"Service entry is missing at {service_entry}",
                )
            )
            continue

        report.checked += 1
        expected_hash = _compute_service_hash(service_entry, hash_spec)
        metadata = _read_metadata(metadata_entry)

        if metadata is None:
            report.issues.append(
                IntegrityIssue(
                    service=service_name,
                    code="missing_metadata",
                    detail=f"Metadata file is missing at {metadata_entry}",
                )
            )
        else:
            configured_metadata_hash = _metadata_hash_value(metadata, hash_spec.id_bytes)
            if configured_metadata_hash != expected_hash:
                fixed_metadata = False
                if fix:
                    _upsert_metadata_hash(metadata, hash_spec.id_bytes, expected_hash)
                    with metadata_entry.open("wb") as metadata_file:
                        metadata_file.write(metadata.SerializeToString())
                    report.fixed += 1
                    fixed_metadata = True

                report.issues.append(
                    IntegrityIssue(
                        service=service_name,
                        code="metadata_hash_mismatch",
                        detail=(
                            f"Configured hash in metadata is '{configured_metadata_hash}', "
                            f"but computed hash is '{expected_hash}'."
                        ),
                        fixed=fixed_metadata,
                    )
                )

        if service_name != expected_hash:
            fixed_names = False
            rename_error = None
            if fix:
                target_service = registry_path / expected_hash
                target_metadata = metadata_path / expected_hash

                rename_error = _safe_rename(service_entry, target_service)
                if not rename_error and metadata_entry.exists():
                    rename_error = _safe_rename(metadata_entry, target_metadata)

                if not rename_error:
                    report.fixed += 1
                    fixed_names = True

            report.issues.append(
                IntegrityIssue(
                    service=service_name,
                    code="service_name_mismatch",
                    detail=(
                        rename_error
                        if rename_error
                        else f"Service name should be '{expected_hash}'."
                    ),
                    fixed=fixed_names,
                )
            )

    if metadata_path.exists():
        registry_set = (
            set(entry.name for entry in registry_path.iterdir())
            if registry_path.exists()
            else set()
        )
        for metadata_entry in sorted([entry.name for entry in metadata_path.iterdir()]):
            if metadata_entry not in registry_set and not resolved_service:
                report.issues.append(
                    IntegrityIssue(
                        service=metadata_entry,
                        code="orphan_metadata",
                        detail="Metadata exists without matching service entry in registry.",
                    )
                )

    return report.as_dict()

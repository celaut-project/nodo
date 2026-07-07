"""Client for uploading registry-hash dependencies to a remote packer service.

Background
----------
When ``nodo pack`` runs the packer *remotely* (in the standalone
celaut-packer-service instead of the local gateway), the packer resolves a
service's declared dependencies against the *packer host's* registry. A
dependency can be:

  * a **local path** (relative to the project) — travels inside the packed zip,
  * a **git URL** (contains ``http``) — cloned by the packer, or
  * a **registry entry** (an already-packed service hash/id).

Local-path and git dependencies reach the remote packer fine (they are in the
zip / fetched on the fly). **Registry-hash dependencies do not** — they live only
in *this* nodo's filesystem registry, and the remote packer's registry is empty,
so packing fails with "Dependency ... not found in the services registry".

This module closes that gap: for each registry-hash dependency it

  1. verifies the dependency exists in *this* nodo's internal registry
     (``REGISTRY``), raising :class:`MissingDependencyError` with a clear message
     if not, and
  2. uploads the packed dependency (service dir + metadata + blocks) to the
     packer service's ``POST /registry/<id>`` endpoint, skipping the upload when
     ``GET /registry/<id>`` reports the packer already has it.

It deliberately mirrors the on-disk layout the vendored ggconf reads:
``{REGISTRY}/<id>/`` (a multiblock dir with ``_.json``), ``{METADATA_REGISTRY}/<id>``
(a file) and ``{BLOCKDIR}/<block>`` (shared, content-addressed block files).
"""
import io
import json
import os
import tarfile
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple


class MissingDependencyError(Exception):
    """A declared dependency could not be resolved as a registry id, a git URL,
    or a local path — so it is missing from this nodo's internal registry and
    cannot be uploaded to the packer service."""


def _registry_dirs() -> Tuple[str, str, str]:
    """(REGISTRY, METADATA_REGISTRY, BLOCKDIR) from this nodo's config — the same
    dirs generate_service_zip / ggconf resolve dependencies against."""
    from src.utils.config import ConfigManager
    cm = ConfigManager()
    return cm.get("REGISTRY"), cm.get("METADATA_REGISTRY"), cm.get("BLOCKDIR")


def _default_get_id(dependency: str) -> str:
    from src.commands.__by_tag import get_id
    return get_id(dependency)


def classify_dependency(
    dependency: str,
    project_dir: str,
    services_dir: str,
    get_id_fn: Callable[[str], str],
) -> Tuple[str, str]:
    """Classify a single pack_config dependency, mirroring the packer's own
    resolution order (registry -> git -> local path).

    Returns one of:
      ("registry", resolved_id) — an already-packed service in this registry,
      ("git", dependency)       — a git URL (contains "http"),
      ("local", abs_path)       — a local path relative to the project,
      ("missing", dependency)   — none of the above (caller should raise).
    """
    # 1. Registry entry — either resolvable via get_id (id or tag) or a direct
    #    hash already present under {REGISTRY}/<id>.
    resolved = ""
    try:
        resolved = get_id_fn(dependency) or ""
    except Exception:
        resolved = ""
    if resolved and os.path.exists(os.path.join(services_dir, resolved)):
        return ("registry", resolved)
    if os.path.exists(os.path.join(services_dir, dependency)):
        return ("registry", dependency)

    # 2. Git URL — travels to the packer as a clone, not our concern to upload.
    if isinstance(dependency, str) and "http" in dependency:
        return ("git", dependency)

    # 3. Local path relative to the project — travels inside the zip.
    if isinstance(dependency, str) and os.path.exists(
        os.path.join(project_dir, dependency)
    ):
        return ("local", os.path.join(project_dir, dependency))

    return ("missing", dependency)


def _iter_dependency_values(pack_config: Dict) -> List[str]:
    """pack_config["dependencies"] may be a dict (NAME->path) or an array."""
    deps = pack_config.get("dependencies")
    if not deps:
        return []
    if isinstance(deps, dict):
        return list(deps.values())
    if isinstance(deps, list):
        return list(deps)
    raise MissingDependencyError(
        f'"dependencies" must be an object or array, got {type(deps).__name__}.'
    )


def build_dependency_bundle(
    service_id: str,
    services_dir: str,
    metadata_dir: str,
    blocks_dir: str,
) -> bytes:
    """Serialise a packed dependency into the gzip-tar bundle the packer
    service's POST /registry/<id> expects:

        service/          -> contents of {services_dir}/<id>/ (multiblock dir)
        metadata          -> {metadata_dir}/<id>, if present
        blocks/<blockid>  -> each block referenced by service/_.json, if present
    """
    service_path = os.path.join(services_dir, service_id)
    if not os.path.isdir(service_path):
        raise MissingDependencyError(
            f"Dependency '{service_id}' not found in the services registry at "
            f"'{services_dir}'. Ensure the dependency is packed locally first."
        )

    # Collect block ids referenced by the multiblock manifest.
    block_ids: List[str] = []
    manifest = os.path.join(service_path, "_.json")
    if os.path.exists(manifest):
        with open(manifest) as f:
            for entry in json.load(f):
                if isinstance(entry, list) and entry:
                    block_ids.append(entry[0])

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(service_path, arcname="service")
        meta_path = os.path.join(metadata_dir, service_id)
        if os.path.isfile(meta_path):
            tf.add(meta_path, arcname="metadata")
        for block in block_ids:
            block_path = os.path.join(blocks_dir, block)
            if os.path.exists(block_path):
                tf.add(block_path, arcname=f"blocks/{block}")
    return buf.getvalue()


def dependency_present(base_url: str, service_id: str, timeout: int = 30) -> bool:
    """GET {base_url}/registry/<id> -> True if the packer already has it."""
    url = base_url.rstrip("/") + f"/registry/{service_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read() or b"{}")
            return bool(body.get("present"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    except urllib.error.URLError:
        # Network/connection problem — let the caller decide (upload will surface
        # a clearer error). Treat as "not present" so we attempt the upload.
        return False


def upload_dependency(
    base_url: str,
    service_id: str,
    bundle: bytes,
    timeout: int = 300,
) -> Dict:
    """POST the dependency bundle to {base_url}/registry/<id>."""
    url = base_url.rstrip("/") + f"/registry/{service_id}"
    req = urllib.request.Request(
        url, data=bundle, method="POST",
        headers={"Content-Type": "application/gzip"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"Packer service rejected dependency '{service_id}' "
            f"({e.code}): {detail}"
        )


def _load_pack_config(project_directory: str) -> Optional[Dict]:
    for candidate in (
        os.path.join(project_directory, ".service", "pack_config.json"),
        os.path.join(project_directory, "pack_config.json"),
    ):
        if os.path.exists(candidate):
            with open(candidate) as f:
                return json.load(f)
    return None


def resolve_and_upload_dependencies(
    project_directory: str,
    packer_service_url: str,
    services_dir: Optional[str] = None,
    metadata_dir: Optional[str] = None,
    blocks_dir: Optional[str] = None,
    get_id_fn: Optional[Callable[[str], str]] = None,
) -> Dict[str, List[str]]:
    """Resolve every pack_config dependency against this nodo's internal registry
    and upload the registry-hash ones to the remote packer service.

    Raises :class:`MissingDependencyError` if a dependency is neither in the
    registry, a git URL, nor a local path.

    Returns a summary: {"uploaded", "already_present", "git", "local"}.
    """
    if services_dir is None or metadata_dir is None or blocks_dir is None:
        services_dir, metadata_dir, blocks_dir = _registry_dirs()
    if get_id_fn is None:
        get_id_fn = _default_get_id

    summary: Dict[str, List[str]] = {
        "uploaded": [], "already_present": [], "git": [], "local": [],
    }

    pack_config = _load_pack_config(project_directory)
    if not pack_config:
        return summary

    for dependency in _iter_dependency_values(pack_config):
        kind, value = classify_dependency(
            dependency, project_directory, services_dir, get_id_fn
        )

        if kind == "missing":
            raise MissingDependencyError(
                f"Dependency '{dependency}' is missing from this nodo's internal "
                f"registry at '{services_dir}' and is neither a git URL nor an "
                "existing local path. Pack or import it locally before packing "
                "this service against the packer service."
            )
        if kind == "git":
            summary["git"].append(value)
            continue
        if kind == "local":
            summary["local"].append(value)
            continue

        # kind == "registry": upload unless the packer already has it.
        service_id = value
        if dependency_present(packer_service_url, service_id):
            summary["already_present"].append(service_id)
            continue
        bundle = build_dependency_bundle(
            service_id, services_dir, metadata_dir, blocks_dir
        )
        upload_dependency(packer_service_url, service_id, bundle)
        summary["uploaded"].append(service_id)

    return summary

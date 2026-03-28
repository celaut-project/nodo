import base64
import hashlib
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bee_rpc.client import Dir, write_to_file

from protos import celaut_pb2
from src.commands.__by_tag import get_id
from src.commands.import_bee import import_bee
from src.utils.config import ConfigManager

API_BASE_URL = "https://api.github.com"


class PublisherError(Exception):
    pass


def _validate_repository_format(repo: str):
    """
    Ensure repository format is exactly: owner/repo
    (one and only one slash, and non-empty owner/repo parts).
    """
    slash_count = repo.count("/")
    if slash_count != 1:
        raise PublisherError(
            "Invalid publisher.REPOSITORY format. "
            f"Expected 'owner/repo' with exactly one '/'. Got: '{repo}'. "
            "Examples: 'octocat/storage-repo', 'my-org/my-bucket-repo'."
        )

    owner, repo_name = repo.split("/", 1)
    if not owner.strip() or not repo_name.strip():
        raise PublisherError(
            "Invalid publisher.REPOSITORY format. "
            f"Owner and repository name are required in 'owner/repo'. Got: '{repo}'."
        )


def _env_from_config(manager: ConfigManager, key: str, fallback: str = "") -> str:
    env_var_name = manager.get(key, "")
    if not env_var_name:
        return fallback
    return os.environ.get(env_var_name, fallback)


def _resolve_token(config: ConfigManager) -> str:
    """
    Resolve publisher token with this priority:
    1) publisher.TOKEN (direct token in config)
    2) env var name in publisher.TOKEN_ENV_VAR
    3) publisher.FALLBACK_TOKEN (direct token in config)
    4) env var name in publisher.FALLBACK_TOKEN_ENV_VAR
    """
    token = str(config.get("publisher.TOKEN", "") or "").strip()
    if token:
        return token

    token = _env_from_config(config, "publisher.TOKEN_ENV_VAR")
    if token:
        return token

    fallback_token = str(config.get("publisher.FALLBACK_TOKEN", "") or "").strip()
    if fallback_token:
        return fallback_token

    fallback_token_env_var = config.get("publisher.FALLBACK_TOKEN_ENV_VAR", "")
    if fallback_token_env_var:
        return os.environ.get(fallback_token_env_var, "")

    return ""


def _safe_upload_id(seed: str) -> str:
    safe_seed = "".join(c if c.isalnum() or c in "._-" else "_" for c in seed)[:40]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{safe_seed}_{timestamp}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _request_with_retry(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout_s: int = 300,
    max_retry: int = 3,
    backoff_s: int = 2,
    **kwargs,
) -> requests.Response:
    for attempt in range(max_retry):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers or {},
                timeout=timeout_s,
                **kwargs,
            )
            if response.status_code in (200, 201):
                return response

            if response.status_code in (409, 422, 502, 503) and attempt < max_retry - 1:
                wait_s = backoff_s * (2 ** attempt)
                print(f"Retrying in {wait_s}s ({method} {url})", flush=True)
                time.sleep(wait_s)
                continue

            raise PublisherError(
                f"HTTP {response.status_code} calling {method} {url}\n{response.text[:500]}"
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt == max_retry - 1:
                raise PublisherError(f"Request failed after {max_retry} attempts: {exc}") from exc
            wait_s = backoff_s * (2 ** attempt)
            print(f"Connection issue. Retrying in {wait_s}s", flush=True)
            time.sleep(wait_s)

    raise PublisherError("Retries exhausted")


class GitHubDataProvider:
    name = "github"

    def __init__(
        self,
        token: str,
        repo: str,
        branch: str,
        timeout_s: int,
        max_retry: int,
        backoff_s: int,
    ):
        _validate_repository_format(repo)

        self.token = token
        self.repo = repo
        self.branch = branch
        self.timeout_s = timeout_s
        self.max_retry = max_retry
        self.backoff_s = backoff_s
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _url(self, path: str) -> str:
        return f"{API_BASE_URL}/repos/{self.repo}/{path}"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        return _request_with_retry(
            method=method,
            url=self._url(path),
            headers=self.headers,
            timeout_s=self.timeout_s,
            max_retry=self.max_retry,
            backoff_s=self.backoff_s,
            **kwargs,
        )

    def create_blob(self, payload: bytes) -> str:
        response = self._request(
            "POST",
            "git/blobs",
            json={
                "content": base64.b64encode(payload).decode(),
                "encoding": "base64",
            },
        )
        return response.json()["sha"]

    def branch_info(self) -> Tuple[Optional[str], Optional[str]]:
        response = requests.get(
            self._url(f"git/ref/heads/{self.branch}"),
            headers=self.headers,
            timeout=30,
        )
        if response.status_code in (404, 409):
            return None, None
        if response.status_code != 200:
            raise PublisherError(f"Could not read branch info: HTTP {response.status_code}")

        commit_sha = response.json()["object"]["sha"]
        commit_response = requests.get(
            self._url(f"git/commits/{commit_sha}"),
            headers=self.headers,
            timeout=30,
        )
        if commit_response.status_code != 200:
            raise PublisherError(f"Could not read commit info: HTTP {commit_response.status_code}")

        return commit_sha, commit_response.json()["tree"]["sha"]

    def create_tree(self, entries: List[Dict], base_tree: Optional[str]) -> str:
        payload = {"tree": entries}
        if base_tree:
            payload["base_tree"] = base_tree
        response = self._request("POST", "git/trees", json=payload)
        return response.json()["sha"]

    def create_commit(self, tree_sha: str, parent_sha: Optional[str], message: str) -> str:
        payload = {
            "message": message,
            "tree": tree_sha,
            "parents": [parent_sha] if parent_sha else [],
        }
        response = self._request("POST", "git/commits", json=payload)
        return response.json()["sha"]

    def update_ref(self, commit_sha: str):
        ref_path = f"git/refs/heads/{self.branch}"
        ref_url = self._url(ref_path)
        response = requests.get(ref_url, headers=self.headers, timeout=30)

        if response.status_code == 200:
            self._request("PATCH", ref_path, json={"sha": commit_sha, "force": False})
            return

        self._request(
            "POST",
            "git/refs",
            json={
                "ref": f"refs/heads/{self.branch}",
                "sha": commit_sha,
            },
        )

    def browse_url(self, path: str) -> str:
        return f"https://github.com/{self.repo}/blob/{self.branch}/{path}"

    def raw_url(self, path: str) -> str:
        return f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{path}"


def _get_publisher_settings(config: ConfigManager, require_token: bool = True) -> Dict:
    provider_name = config.get("publisher.PROVIDER", "github").lower()
    token = _resolve_token(config)

    repo = config.get("publisher.REPOSITORY", "")
    branch = config.get("publisher.BRANCH", "main")
    chunk_size_mb = int(config.get("publisher.CHUNK_SIZE_MB", 24))
    timeout_s = int(config.get("publisher.TIMEOUT_SECONDS", 300))
    max_retry = int(config.get("publisher.MAX_RETRY", 3))
    backoff_s = int(config.get("publisher.BACKOFF_SECONDS", 2))
    uploads_prefix = config.get("publisher.UPLOADS_PREFIX", "uploads").strip("/") or "uploads"
    output_dir = config.get("publisher.DOWNLOAD_OUTPUT_DIR", ".")
    keep_artifacts = bool(config.get("publisher.KEEP_DOWNLOADED_FILE", True))
    auto_import = bool(config.get("publisher.AUTO_IMPORT_SERVICE_ON_DOWNLOAD", True))

    if provider_name != "github":
        raise PublisherError(f"Unsupported publisher provider '{provider_name}'.")
    if require_token and not token:
        raise PublisherError(
            "Missing publisher token. Set publisher.TOKEN or configure publisher.TOKEN_ENV_VAR."
        )
    if not repo:
        raise PublisherError("Missing publisher repository in config key publisher.REPOSITORY.")
    _validate_repository_format(repo)

    return {
        "provider_name": provider_name,
        "token": token,
        "repo": repo,
        "branch": branch,
        "chunk_size_mb": chunk_size_mb,
        "timeout_s": timeout_s,
        "max_retry": max_retry,
        "backoff_s": backoff_s,
        "uploads_prefix": uploads_prefix,
        "output_dir": output_dir,
        "keep_artifacts": keep_artifacts,
        "auto_import": auto_import,
    }


def _service_export_generator(service_id: str):
    config = ConfigManager()
    metadata_registry = config.get("METADATA_REGISTRY")
    service_registry = config.get("REGISTRY")

    yield Dir(
        dir=os.path.join(metadata_registry, service_id),
        _type=celaut_pb2.Metadata,
    )
    yield Dir(
        dir=os.path.join(service_registry, service_id),
        _type=celaut_pb2.Service,
    )


def _export_service_to_bee(service_ref: str) -> Tuple[str, Path]:
    service_id = get_id(service_ref)
    if not service_id:
        raise PublisherError(f"Service '{service_ref}' was not found by id or tag.")

    with tempfile.TemporaryDirectory(prefix="nodo_publish_") as temp_dir:
        output_file = write_to_file(
            path=temp_dir,
            file_name=service_id[:12],
            extension="celaut.bee",
            input=_service_export_generator(service_id),
            indices={
                1: celaut_pb2.Metadata,
                2: celaut_pb2.Service,
            },
        )

        fd, artifact_path = tempfile.mkstemp(prefix=f"{service_id}_", suffix=".celaut.bee")
        os.close(fd)
        final_path = Path(artifact_path)
        Path(output_file).replace(final_path)

    return service_id, final_path


def _upload_file(
    source_path: Path,
    provider: GitHubDataProvider,
    chunk_size_mb: int,
    uploads_prefix: str,
    upload_id: Optional[str] = None,
    service_id: Optional[str] = None,
) -> Dict:
    if chunk_size_mb > 95:
        print("Chunk size above 95 MB is not valid for GitHub blobs. Using 95 MB.", flush=True)
        chunk_size_mb = 95
    if chunk_size_mb <= 0:
        raise PublisherError("Chunk size must be greater than 0.")

    chunk_size = chunk_size_mb * 1024 * 1024
    file_size = source_path.stat().st_size
    total_chunks = max(1, math.ceil(file_size / chunk_size))
    resolved_upload_id = upload_id or _safe_upload_id(source_path.stem)
    folder = f"{uploads_prefix}/{resolved_upload_id}"

    print(f"Publishing '{source_path.name}' to {provider.repo}:{provider.branch}", flush=True)
    print(f"Upload id: {resolved_upload_id} | Chunks: {total_chunks}", flush=True)

    full_hash = _sha256_file(source_path)
    tree_entries: List[Dict] = []
    chunk_manifest_entries: List[Dict] = []

    with source_path.open("rb") as source:
        for index in range(total_chunks):
            chunk_data = source.read(chunk_size)
            chunk_name = f"chunk_{index:04d}"
            chunk_path = f"{folder}/{chunk_name}"
            chunk_hash = hashlib.sha256(chunk_data).hexdigest()
            blob_sha = provider.create_blob(chunk_data)

            tree_entries.append(
                {
                    "path": chunk_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            )
            chunk_manifest_entries.append(
                {
                    "index": index,
                    "filename": chunk_name,
                    "size": len(chunk_data),
                    "sha256": chunk_hash,
                }
            )
            print(f"Uploaded chunk {index + 1}/{total_chunks} ({blob_sha[:8]})", flush=True)

    manifest = {
        "version": 1,
        "kind": "nodo_service_publish",
        "filename": source_path.name,
        "file_size": file_size,
        "sha256": full_hash,
        "chunk_size": chunk_size,
        "total_chunks": total_chunks,
        "upload_id": resolved_upload_id,
        "provider": provider.name,
        "repo": provider.repo,
        "branch": provider.branch,
        "service_id": service_id or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "chunks": chunk_manifest_entries,
    }

    manifest_blob_sha = provider.create_blob(
        json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    )
    tree_entries.append(
        {
            "path": f"{folder}/manifest.json",
            "mode": "100644",
            "type": "blob",
            "sha": manifest_blob_sha,
        }
    )

    head_commit_sha, base_tree_sha = provider.branch_info()
    tree_sha = provider.create_tree(tree_entries, base_tree_sha)
    commit_sha = provider.create_commit(
        tree_sha=tree_sha,
        parent_sha=head_commit_sha,
        message=f"Publish service artifact {source_path.name}",
    )
    provider.update_ref(commit_sha)

    manifest_url = provider.raw_url(f"{folder}/manifest.json")
    browse_manifest_url = provider.browse_url(f"{folder}/manifest.json")
    return {
        "manifest": manifest,
        "manifest_url": manifest_url,
        "browse_manifest_url": browse_manifest_url,
        "upload_id": resolved_upload_id,
        "commit_sha": commit_sha,
    }


def _fetch_bytes(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout_s: int = 300,
    max_retry: int = 3,
    backoff_s: int = 2,
) -> bytes:
    response = _request_with_retry(
        method="GET",
        url=url,
        headers=headers or {},
        timeout_s=timeout_s,
        max_retry=max_retry,
        backoff_s=backoff_s,
    )
    return response.content


def publish_service(
    service_ref: str,
    upload_id: Optional[str] = None,
    chunk_size_mb: Optional[int] = None,
) -> Dict:
    config = ConfigManager()
    settings = _get_publisher_settings(config, require_token=True)

    provider = GitHubDataProvider(
        token=settings["token"],
        repo=settings["repo"],
        branch=settings["branch"],
        timeout_s=settings["timeout_s"],
        max_retry=settings["max_retry"],
        backoff_s=settings["backoff_s"],
    )

    service_id, service_file_path = _export_service_to_bee(service_ref)
    try:
        result = _upload_file(
            source_path=service_file_path,
            provider=provider,
            chunk_size_mb=chunk_size_mb or settings["chunk_size_mb"],
            uploads_prefix=settings["uploads_prefix"],
            upload_id=upload_id,
            service_id=service_id,
        )
    finally:
        if service_file_path.exists():
            service_file_path.unlink()

    print("Publish completed successfully.", flush=True)
    print(f"Service id: {service_id}", flush=True)
    print(f"Manifest URL: {result['manifest_url']}", flush=True)
    print(f"Manifest browser URL: {result['browse_manifest_url']}", flush=True)
    print(f"Download command: nodo download {result['manifest_url']}", flush=True)
    return result


def download_from_manifest_url(manifest_url: str, output_dir: Optional[str] = None) -> Dict:
    config = ConfigManager()
    settings = _get_publisher_settings(config, require_token=False)
    target_dir = Path(output_dir or settings["output_dir"]).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    headers: Dict[str, str] = {}
    if settings["token"]:
        headers["Authorization"] = f"token {settings['token']}"

    manifest_bytes = _fetch_bytes(
        manifest_url,
        headers=headers,
        timeout_s=settings["timeout_s"],
        max_retry=settings["max_retry"],
        backoff_s=settings["backoff_s"],
    )
    manifest = json.loads(manifest_bytes)

    required_fields = [
        "upload_id",
        "filename",
        "sha256",
        "total_chunks",
        "chunks",
    ]
    missing = [field for field in required_fields if field not in manifest]
    if missing:
        raise PublisherError(f"Manifest is missing required fields: {', '.join(missing)}")

    repo = manifest.get("repo", settings["repo"])
    branch = manifest.get("branch", settings["branch"])
    upload_id = manifest["upload_id"]
    raw_base = f"https://raw.githubusercontent.com/{repo}/{branch}/{settings['uploads_prefix']}/{upload_id}"
    output_path = target_dir / manifest["filename"]

    sha_total = hashlib.sha256()
    with output_path.open("wb") as destination:
        for chunk_meta in manifest["chunks"]:
            chunk_name = chunk_meta["filename"]
            expected_size = int(chunk_meta["size"])
            expected_hash = chunk_meta["sha256"]
            chunk_url = f"{raw_base}/{chunk_name}"
            data = _fetch_bytes(
                chunk_url,
                headers=headers,
                timeout_s=settings["timeout_s"],
                max_retry=settings["max_retry"],
                backoff_s=settings["backoff_s"],
            )

            if len(data) != expected_size:
                raise PublisherError(
                    f"Chunk {chunk_name} size mismatch: {len(data)} != {expected_size}"
                )
            hash_value = hashlib.sha256(data).hexdigest()
            if hash_value != expected_hash:
                raise PublisherError(
                    f"Chunk {chunk_name} hash mismatch: {hash_value} != {expected_hash}"
                )

            destination.write(data)
            sha_total.update(data)
            print(f"Downloaded {chunk_name}", flush=True)

    final_hash = sha_total.hexdigest()
    if final_hash != manifest["sha256"]:
        raise PublisherError(
            f"Final file hash mismatch: {final_hash} != {manifest['sha256']}"
        )

    imported_service_id = None
    if settings["auto_import"]:
        imported_service_id = import_bee(str(output_path))
        if imported_service_id:
            print(f"Service imported successfully: {imported_service_id}", flush=True)

    if not settings["keep_artifacts"] and output_path.exists():
        output_path.unlink()
        print(f"Removed downloaded artifact: {output_path}", flush=True)

    print("Download completed successfully.", flush=True)
    return {
        "manifest": manifest,
        "output_path": str(output_path),
        "service_id": imported_service_id,
    }

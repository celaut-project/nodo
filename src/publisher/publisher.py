import base64
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from uuid import uuid4

import requests
from bee_rpc.client import Dir, write_to_file

from protos import celaut_pb2
from src.commands.__by_tag import get_id
from src.commands.import_bee import import_bee
from src.utils.config import ConfigManager
from src.utils.hashing import get_configured_hash_spec, hash_file

API_BASE_URL = "https://api.github.com"
RETRYABLE_HTTP_STATUS_CODES = {401, 409, 422, 429, 500, 502, 503, 504}
DEFAULT_SOURCE_APPLICATION_WEB_PAGE = "https://reputation-systems.github.io/source-application?tab=add"


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

            if (
                response.status_code in RETRYABLE_HTTP_STATUS_CODES
                and attempt < max_retry - 1
            ):
                wait_s = backoff_s * (2 ** attempt)
                print(
                    f"Retrying after HTTP {response.status_code} in {wait_s}s "
                    f"({method} {url})",
                    flush=True,
                )
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

    def ensure_repository_initialized(self):
        """
        Git Data API cannot create blobs in an empty repository.
        If the target branch is missing/empty, bootstrap it with a tiny file commit.
        """
        ref_url = self._url(f"git/ref/heads/{self.branch}")
        response = requests.get(ref_url, headers=self.headers, timeout=30)
        if response.status_code == 200:
            return
        if response.status_code not in (404, 409):
            raise PublisherError(
                f"Unable to inspect branch '{self.branch}' (HTTP {response.status_code})."
            )

        init_path = ".nodo-publisher-init"
        init_content = (
            f"Initialized by nodo publisher at {datetime.now(timezone.utc).isoformat()}\n"
        ).encode()
        payload = {
            "message": f"Initialize branch '{self.branch}' for publisher",
            "content": base64.b64encode(init_content).decode(),
            "branch": self.branch,
        }
        init_response = requests.put(
            self._url(f"contents/{init_path}"),
            headers=self.headers,
            timeout=30,
            json=payload,
        )
        if init_response.status_code in (200, 201):
            print(
                f"Repository branch '{self.branch}' initialized for publishing.",
                flush=True,
            )
            return

        body = init_response.text[:500]
        raise PublisherError(
            "Could not initialize repository for publishing. "
            f"HTTP {init_response.status_code}: {body}"
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
    try:
        hash_spec = get_configured_hash_spec(config)
    except ValueError as exc:
        raise PublisherError(f"Invalid hashing.HASH configuration: {exc}") from exc
    # Raw configured value (may be empty). We deliberately do NOT fall back to
    # DEFAULT_SOURCE_APPLICATION_WEB_PAGE here: an unset web page is a meaningful
    # signal to the publish flow — it selects the level-4 "register manually"
    # message rather than silently pointing at the public default page
    # (see _announce_source_registration). The default is still used as the read
    # base by src/core_services/source_application.py.
    source_application_web_page = str(
        config.get("publisher.SOURCE_APPLICATION_WEB_PAGE", "") or ""
    ).strip()
    # When enabled AND a source-application core-service instance is running, the
    # publish flow submits the source transaction directly through that instance's
    # API (signed with the node wallet seed) instead of printing a click-to-add link.
    auto_publish_tx = bool(config.get("publisher.AUTO_PUBLISH_TX", False))
    content_format = str(config.get("publisher.CONTENT_FORMAT", ".grpcbb") or "").strip() or ".grpcbb"
    raw_format = str(config.get("publisher.RAW_FORMAT", ".celaut") or "").strip() or ".celaut"
    if not content_format.startswith("."):
        content_format = f".{content_format}"
    if not raw_format.startswith("."):
        raw_format = f".{raw_format}"
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
        "hash_spec": hash_spec,
        "source_application_web_page": source_application_web_page,
        "auto_publish_tx": auto_publish_tx,
        "content_format": content_format,
        "raw_format": raw_format,
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
    service_id: str,
) -> Dict:
    provider.ensure_repository_initialized()

    if chunk_size_mb > 95:
        print("Chunk size above 95 MB is not valid for GitHub blobs. Using 95 MB.", flush=True)
        chunk_size_mb = 95
    if chunk_size_mb <= 0:
        raise PublisherError("Chunk size must be greater than 0.")

    chunk_size = chunk_size_mb * 1024 * 1024
    file_size = source_path.stat().st_size
    total_chunks = max(1, math.ceil(file_size / chunk_size))
    folder = f"{uploads_prefix}/{service_id}"

    print(f"Publishing '{source_path.name}' to {provider.repo}:{provider.branch}", flush=True)
    print(f"Service hash: {service_id} | Chunks: {total_chunks}", flush=True)

    tree_entries: List[Dict] = []
    manifest_lines: List[str] = []

    with source_path.open("rb") as source:
        for index in range(total_chunks):
            chunk_data = source.read(chunk_size)
            chunk_name = f"chunk_{index:04d}"
            chunk_path = f"{folder}/{chunk_name}"
            blob_sha = provider.create_blob(chunk_data)

            tree_entries.append(
                {
                    "path": chunk_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            )
            manifest_lines.append(provider.raw_url(chunk_path))
            print(f"Uploaded chunk {index + 1}/{total_chunks} ({blob_sha[:8]})", flush=True)

    manifest_plain = "\n".join(manifest_lines) + "\n"
    manifest_blob_sha = provider.create_blob(manifest_plain.encode("utf-8"))
    tree_entries.append(
        {
            "path": f"{folder}/manifest",
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

    manifest_url = provider.raw_url(f"{folder}/manifest")
    browse_manifest_url = provider.browse_url(f"{folder}/manifest")
    return {
        "manifest": manifest_plain,
        "manifest_url": manifest_url,
        "browse_manifest_url": browse_manifest_url,
        "service_id": service_id,
        "total_chunks": total_chunks,
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


def _build_source_application_prefilled_url(
    base_url: str,
    file_hash: str,
    content_hash: str,
    hash_function_id: str,
    url_link: str,
    content_format: str,
    raw_format: str,
    is_chunked: bool = True,
) -> str:
    parsed = urlsplit(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "fileHash": file_hash,
            "contentHash": content_hash,
            "hashFunctionId": hash_function_id,
            "urlLink": url_link,
            "contentFormat": content_format,
            "isChunked": "true" if is_chunked else "false",
        }
    )
    if raw_format != content_format:
        query["rawFormat"] = raw_format
        query["rawHash"] = file_hash
    else:
        query.pop("rawFormat", None)
        query.pop("rawHash", None)

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


# Provisional write route on a running source-application instance's API. Like the
# read route in src/core_services/source_application.py (``<base>/sources/<id>``),
# this contract must be confirmed against the deployed source-application service
# before the AUTO_PUBLISH_TX path leaves DRAFT.
SOURCE_APPLICATION_PUBLISH_ROUTE = "publish"


def _resolve_source_application_endpoint() -> Optional[str]:
    """Return the endpoint of a *running* source-application core-service instance.

    Requires a configured (non-placeholder) ``source-application`` id under
    ``core_services`` AND a currently-running instance of it; returns ``None``
    otherwise. Detection only — it never downloads or launches the service, so a
    publish never triggers heavy side effects. Fully defensive: never raises.
    """
    try:
        from src.core_services import SOURCE_APPLICATION, get_core_service_id
        from src.core_services.runtime import find_running_endpoint

        source_application_id = get_core_service_id(SOURCE_APPLICATION)
        if not source_application_id:
            return None
        return find_running_endpoint(source_application_id)
    except Exception:
        # Missing core_services infra / db / parse error — treat as "no instance".
        return None


def _submit_source_via_instance_api(
    endpoint: str,
    *,
    seed: str,
    file_hash: str,
    content_hash: str,
    hash_function_id: str,
    manifest_url: str,
    content_format: str,
    raw_format: str,
    timeout_s: int,
    max_retry: int,
    backoff_s: int,
    is_chunked: bool = True,
) -> bool:
    """AUTO_PUBLISH_TX: hand the source + the node wallet seed to the running
    source-application instance so it signs and submits the on-chain source
    transaction directly (no manual click-to-add step).

    The seed (``ledgers.ergo.WALLET_MNEMONIC``) is sent to the *local, on-node*
    source-application instance, which acts as the seed signer. Provisional write
    contract — confirm the route/payload against the deployed service. Best-effort:
    returns ``False`` on any failure so the caller falls back to a registration link.
    """
    url = f"{endpoint.rstrip('/')}/{SOURCE_APPLICATION_PUBLISH_ROUTE}"
    payload = {
        "fileHash": file_hash,
        "contentHash": content_hash,
        "hashFunctionId": hash_function_id,
        "urlLink": manifest_url,
        "contentFormat": content_format,
        "isChunked": is_chunked,
        "seed": seed,
    }
    if raw_format != content_format:
        payload["rawFormat"] = raw_format
        payload["rawHash"] = file_hash

    try:
        _request_with_retry(
            "POST",
            url,
            headers={"Content-Type": "application/json"},
            timeout_s=timeout_s,
            max_retry=max_retry,
            backoff_s=backoff_s,
            json=payload,
        )
        return True
    except PublisherError as exc:
        print(f"⚠️  Auto source-tx submit failed: {exc}", flush=True)
        return False
    except Exception as exc:  # defensive: the auto path must never break publish
        print(f"⚠️  Auto source-tx submit errored: {exc}", flush=True)
        return False


def _print_click_to_add(
    base_url: str,
    *,
    file_hash: str,
    content_hash: str,
    hash_function_id: str,
    manifest_url: str,
    content_format: str,
    raw_format: str,
) -> None:
    """Print the manual "click to add source" block against ``base_url``."""
    prefilled_url = _build_source_application_prefilled_url(
        base_url=base_url,
        file_hash=file_hash,
        content_hash=content_hash,
        hash_function_id=hash_function_id,
        url_link=manifest_url,
        content_format=content_format,
        raw_format=raw_format,
        is_chunked=True,
    )
    print("Register this source in Source Application:", flush=True)
    print(f"- Source application URL: {base_url}", flush=True)
    print(f"- Source application prefilled URL: {prefilled_url}", flush=True)
    print(f"- Manifest URL: {manifest_url}", flush=True)
    print(f"- File hash: {file_hash}", flush=True)
    print(f"- Content hash: {content_hash}", flush=True)

    BOLD = "\033[1m"
    GREEN = "\033[92m"
    RESET = "\033[0m"
    print(f"\n\n{BOLD}{GREEN}👉 CLICK TO ADD SOURCE IN ERGO:{RESET}", flush=True)
    print(f"{GREEN}{prefilled_url}{RESET}\n", flush=True)


def _announce_source_registration(
    settings: Dict,
    *,
    service_id: str,
    content_hash: str,
    manifest_url: str,
) -> None:
    """Decide, across four levels, how the freshly-published source is registered.

    1. ``AUTO_PUBLISH_TX`` on **and** a running source-application instance →
       submit the source transaction through the instance API, signed with the
       node wallet seed (no manual step).
    2. A running instance but ``AUTO_PUBLISH_TX`` off (or the auto submit fell
       back) → print a click-to-add link against the instance's web page.
    3. No instance, but ``publisher.SOURCE_APPLICATION_WEB_PAGE`` is set → print a
       click-to-add link against that page (the pre-existing behaviour).
    4. No instance and no web page configured → a manual-registration message.
    """
    hash_function_id = settings["hash_spec"].id_bytes.hex()
    endpoint = _resolve_source_application_endpoint()
    web_page = settings["source_application_web_page"]

    # Level 1 — auto-submit via the running instance, signed with the node seed.
    if endpoint and settings["auto_publish_tx"]:
        seed = str(ConfigManager().get("ledgers.ergo.WALLET_MNEMONIC", "") or "").strip()
        if not seed:
            print(
                "⚠️  AUTO_PUBLISH_TX is enabled but ledgers.ergo.WALLET_MNEMONIC is empty — "
                "cannot sign the source transaction. Falling back to a registration link.",
                flush=True,
            )
        else:
            print(
                "AUTO_PUBLISH_TX enabled — submitting the source transaction via the running "
                "source-application instance (signed with the node wallet seed)...",
                flush=True,
            )
            submitted = _submit_source_via_instance_api(
                endpoint,
                seed=seed,
                file_hash=service_id,
                content_hash=content_hash,
                hash_function_id=hash_function_id,
                manifest_url=manifest_url,
                content_format=settings["content_format"],
                raw_format=settings["raw_format"],
                timeout_s=settings["timeout_s"],
                max_retry=settings["max_retry"],
                backoff_s=settings["backoff_s"],
            )
            if submitted:
                print(
                    "✅ Source transaction submitted directly via the source-application instance.",
                    flush=True,
                )
                return
            print(
                "↩️  Auto submit unsuccessful — showing a manual registration link instead.",
                flush=True,
            )

    # Level 2 — a running instance exists: link against its web page.
    if endpoint:
        _print_click_to_add(
            endpoint,
            file_hash=service_id,
            content_hash=content_hash,
            hash_function_id=hash_function_id,
            manifest_url=manifest_url,
            content_format=settings["content_format"],
            raw_format=settings["raw_format"],
        )
        return

    # Level 3 — no instance, but a source-application web page is configured.
    if web_page:
        _print_click_to_add(
            web_page,
            file_hash=service_id,
            content_hash=content_hash,
            hash_function_id=hash_function_id,
            manifest_url=manifest_url,
            content_format=settings["content_format"],
            raw_format=settings["raw_format"],
        )
        return

    # Level 4 — nothing configured: tell the user how to register manually.
    print(
        "ℹ️  Source uploaded, but no source-application instance is running and "
        "publisher.SOURCE_APPLICATION_WEB_PAGE is not set. Register the source manually "
        "using the file hash, content hash and manifest URL above.",
        flush=True,
    )


def publish_service(
    service_ref: str
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
    content_hash = hash_file(service_file_path, settings["hash_spec"]).hex()
    try:
        result = _upload_file(
            source_path=service_file_path,
            provider=provider,
            chunk_size_mb=settings["chunk_size_mb"],
            uploads_prefix=settings["uploads_prefix"],
            service_id=service_id
        )
    finally:
        if service_file_path.exists():
            service_file_path.unlink()

    print("Publish completed successfully.", flush=True)
    print(f"Service id: {service_id}", flush=True)
    print(f"File hash: {service_id}", flush=True)
    print(f"Content hash: {content_hash}", flush=True)
    print(f"Manifest URL: {result['manifest_url']}", flush=True)
    print(f"Manifest browser URL: {result['browse_manifest_url']}", flush=True)
    print(f"Download command: nodo download {result['manifest_url']}", flush=True)

    # Four-level source registration (auto-tx via instance / instance link /
    # configured web page link / manual message). See _announce_source_registration.
    _announce_source_registration(
        settings,
        service_id=service_id,
        content_hash=content_hash,
        manifest_url=result["manifest_url"],
    )
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
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublisherError("Manifest must be UTF-8 plain text.") from exc

    chunk_urls = [line.strip() for line in manifest_text.splitlines() if line.strip()]
    if not chunk_urls:
        raise PublisherError("Manifest is empty. It must contain one chunk URL per line.")

    path_parts = [part for part in urlparse(manifest_url).path.split("/") if part]
    if len(path_parts) < 2:
        raise PublisherError(f"Invalid manifest URL path: {manifest_url}")

    uuid = uuid4().hex[:8]
    output_path = target_dir / f"{uuid}.celaut.bee"

    with output_path.open("wb") as destination:
        for index, chunk_url in enumerate(chunk_urls):
            data = _fetch_bytes(
                chunk_url,
                headers=headers,
                timeout_s=settings["timeout_s"],
                max_retry=settings["max_retry"],
                backoff_s=settings["backoff_s"],
            )
            destination.write(data)
            print(f"Downloaded chunk {index + 1}/{len(chunk_urls)}", flush=True)

    imported_service_id = None
    if settings["auto_import"]:
        imported_service_id = import_bee(str(output_path))
        if imported_service_id:
            print(f"Service imported successfully: {imported_service_id}", flush=True)

    if not settings["keep_artifacts"] and output_path.exists():
        output_path.unlink()
        print(f"Removed downloaded artifact: {output_path}", flush=True)
    elif output_path.exists() and imported_service_id:
            final_output_path = target_dir / f"{imported_service_id}.celaut.bee"
            output_path.rename(final_output_path)
            print(f"Downloaded artifact kept at: {final_output_path}", flush=True)

    print("Download completed successfully.", flush=True)
    if imported_service_id:
        print(f"\nRun it with:\n   nodo execute {imported_service_id}\n(--remote in case you are in a ssh session)", flush=True)
    return {
        "manifest": chunk_urls,
        "manifest_url": manifest_url,
        "service_hash": imported_service_id,
        "output_path": str(output_path),
        "service_id": imported_service_id,
    }

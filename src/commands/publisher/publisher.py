import json
import math
import os
import tempfile
import time
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
from src.utils.service_content import compute_id

API_BASE_URL = "https://api.github.com"
UPLOADS_BASE_URL = "https://uploads.github.com"
RETRYABLE_HTTP_STATUS_CODES = {401, 409, 422, 429, 500, 502, 503, 504}
DEFAULT_SOURCE_APPLICATION_WEB_PAGE = "https://reputation-systems.github.io/source-application?tab=add"

# GitHub refuses a release asset of 2 GiB or more.
MAX_ASSET_SIZE_MB = 2047
DEFAULT_SPLIT_SIZE_MB = 2000
DEFAULT_PART_SIZE_MB = 1000
RELEASE_TAG_PREFIX = "celaut-"
MANIFEST_ASSET_NAME = "manifest"


class PublisherError(Exception):
    def __init__(self, message: str = "", status_code: Optional[int] = None):
        super().__init__(message)
        # HTTP status of the failed call, when there was one.
        self.status_code = status_code


def _token_permission_message(repo: str) -> str:
    return (
        f"The publisher token cannot create releases in '{repo}'. "
        "nodo publish uploads the service as a GitHub Release asset, so the token needs "
        "write access to the repository contents: for a fine-grained token, "
        "'Contents: Read and write' on this repository; for a classic token, the "
        "'repo' scope (or 'public_repo' for a public repository). "
        "Set it in publisher.TOKEN or in the variable named by publisher.TOKEN_ENV_VAR."
    )


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
            if 200 <= response.status_code < 300:
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
                f"HTTP {response.status_code} calling {method} {url}\n{response.text[:500]}",
                status_code=response.status_code,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt == max_retry - 1:
                raise PublisherError(f"Request failed after {max_retry} attempts: {exc}") from exc
            wait_s = backoff_s * (2 ** attempt)
            print(f"Connection issue. Retrying in {wait_s}s", flush=True)
            time.sleep(wait_s)

    raise PublisherError("Retries exhausted")


class GitHubReleaseProvider:
    """Publishes files as assets of a GitHub Release.

    Every call goes through ``_request_with_retry``, so ``publisher.TIMEOUT_SECONDS``,
    ``MAX_RETRY`` and ``BACKOFF_SECONDS`` apply to the release API as they did to the
    Git Data API before.
    """

    name = "github"

    def __init__(
        self,
        token: str,
        repo: str,
        target: str,
        timeout_s: int,
        max_retry: int,
        backoff_s: int,
    ):
        _validate_repository_format(repo)

        self.token = token
        self.repo = repo
        # Branch or commit the release tag is created on. Empty: the default branch.
        self.target = target
        self.timeout_s = timeout_s
        self.max_retry = max_retry
        self.backoff_s = backoff_s
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _url(self, path: str) -> str:
        base = f"{API_BASE_URL}/repos/{self.repo}"
        return f"{base}/{path}" if path else base

    def _request(self, method: str, path_or_url: str, **kwargs) -> requests.Response:
        url = path_or_url if path_or_url.startswith("https://") else self._url(path_or_url)
        kwargs.setdefault("timeout_s", self.timeout_s)
        kwargs.setdefault("max_retry", self.max_retry)
        kwargs.setdefault("backoff_s", self.backoff_s)
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}))
        return _request_with_retry(method=method, url=url, headers=headers, **kwargs)

    def check_access(self):
        """Fail early, with a useful message, when the token cannot create releases."""
        try:
            repo_info = self._request("GET", "").json()
        except PublisherError as exc:
            if exc.status_code in (401, 403, 404):
                raise PublisherError(_token_permission_message(self.repo)) from exc
            raise
        permissions = repo_info.get("permissions")
        if isinstance(permissions, dict) and not permissions.get("push", False):
            raise PublisherError(_token_permission_message(self.repo))

        try:
            self._request("GET", "commits", params={"per_page": 1})
        except PublisherError as exc:
            if exc.status_code == 409:
                raise PublisherError(
                    f"Repository '{self.repo}' is empty. A release tag needs a commit to "
                    "point at: add one commit (for example a README) to the repository "
                    "and publish again. nodo publish does not commit to the repository."
                ) from exc
            raise

    def find_release(self, tag: str) -> Optional[Dict]:
        """The release with ``tag``, draft or published, or None."""
        try:
            return self._request("GET", f"releases/tags/{tag}").json()
        except PublisherError as exc:
            if exc.status_code != 404:
                raise
        # releases/tags/<tag> does not see drafts: a run that was killed before it
        # published leaves one behind, so look for it in the list.
        page = 1
        while True:
            releases = self._request(
                "GET", "releases", params={"per_page": 100, "page": page}
            ).json()
            for release in releases:
                if release.get("tag_name") == tag:
                    return release
            if len(releases) < 100:
                return None
            page += 1

    def create_draft_release(self, tag: str, title: str, body: str) -> Dict:
        payload = {
            "tag_name": tag,
            "name": title,
            "body": body,
            "draft": True,
        }
        if self.target:
            payload["target_commitish"] = self.target
        try:
            return self._request("POST", "releases", json=payload).json()
        except PublisherError as exc:
            if exc.status_code in (403, 404):
                raise PublisherError(_token_permission_message(self.repo)) from exc
            raise

    def update_release(self, release_id: int, **fields) -> Dict:
        return self._request("PATCH", f"releases/{release_id}", json=fields).json()

    def delete_release(self, release_id: int):
        self._request("DELETE", f"releases/{release_id}")

    def list_assets(self, release_id: int) -> List[Dict]:
        assets: List[Dict] = []
        page = 1
        while True:
            batch = self._request(
                "GET",
                f"releases/{release_id}/assets",
                params={"per_page": 100, "page": page},
            ).json()
            assets.extend(batch)
            if len(batch) < 100:
                return assets
            page += 1

    def delete_asset(self, asset_id: int):
        self._request("DELETE", f"releases/assets/{asset_id}")

    def upload_asset(self, release_id: int, name: str, body_factory, size: int) -> Dict:
        """Upload one asset. ``body_factory`` returns a fresh body for every attempt.

        An attempt that fails part way can leave a half-uploaded asset with that name,
        and GitHub then refuses the next attempt with 422. So retries are done here:
        each one first removes an asset left with the same name.
        """
        url = f"{UPLOADS_BASE_URL}/repos/{self.repo}/releases/{release_id}/assets"
        for attempt in range(self.max_retry):
            if attempt:
                for asset in self.list_assets(release_id):
                    if asset.get("name") == name:
                        self.delete_asset(asset["id"])
            try:
                return self._request(
                    "POST",
                    url,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(size),
                    },
                    params={"name": name},
                    data=body_factory(),
                    max_retry=1,
                ).json()
            except PublisherError as exc:
                retryable = exc.status_code is None or exc.status_code in RETRYABLE_HTTP_STATUS_CODES
                if not retryable or attempt == self.max_retry - 1:
                    raise
                wait_s = self.backoff_s * (2 ** attempt)
                print(f"Retrying upload of '{name}' in {wait_s}s ({exc})", flush=True)
                time.sleep(wait_s)
        raise PublisherError("Retries exhausted")

    def download_url(self, tag: str, name: str) -> str:
        """Stable public URL of a release asset. It works only once the release is published."""
        return f"https://github.com/{self.repo}/releases/download/{tag}/{name}"


def _get_publisher_settings(config: ConfigManager, require_token: bool = True) -> Dict:
    provider_name = config.get("publisher.PROVIDER", "github").lower()
    token = _resolve_token(config)

    repo = config.get("publisher.REPOSITORY", "")
    # Only the branch the release tag is created on; the file data is never committed.
    # Empty: the repository's default branch.
    branch = str(config.get("publisher.BRANCH", "") or "").strip()
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
    split_size_mb = int(config.get("publisher.SPLIT_SIZE_MB", DEFAULT_SPLIT_SIZE_MB))
    part_size_mb = int(config.get("publisher.PART_SIZE_MB", DEFAULT_PART_SIZE_MB))
    for key, value in (("SPLIT_SIZE_MB", split_size_mb), ("PART_SIZE_MB", part_size_mb)):
        if not 0 < value <= MAX_ASSET_SIZE_MB:
            raise PublisherError(
                f"publisher.{key} must be between 1 and {MAX_ASSET_SIZE_MB} "
                f"(GitHub refuses a release asset of 2 GiB or more). Got: {value}."
            )
    timeout_s = int(config.get("publisher.TIMEOUT_SECONDS", 300))
    max_retry = int(config.get("publisher.MAX_RETRY", 3))
    backoff_s = int(config.get("publisher.BACKOFF_SECONDS", 2))
    output_dir = config.get("publisher.DOWNLOAD_OUTPUT_DIR", ".")
    keep_artifacts = bool(config.get("publisher.KEEP_DOWNLOADED_FILE", True))
    auto_import = bool(config.get("publisher.AUTO_IMPORT_SERVICE_ON_DOWNLOAD", True))

    if provider_name != "github":
        raise PublisherError(f"Unsupported publisher provider '{provider_name}'.")
    if require_token and not token:
        raise PublisherError(
            "Missing publisher token. Set publisher.TOKEN or configure publisher.TOKEN_ENV_VAR. "
            "The token must be allowed to create releases in publisher.REPOSITORY "
            "('Contents: Read and write' for a fine-grained token, 'repo' or "
            "'public_repo' scope for a classic one)."
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
        "split_size_mb": split_size_mb,
        "part_size_mb": part_size_mb,
        "timeout_s": timeout_s,
        "max_retry": max_retry,
        "backoff_s": backoff_s,
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

        fd, artifact_path = tempfile.mkstemp(
            prefix=f"{service_id}_",
            suffix=".celaut.bee",
        )
        os.close(fd)

        final_path = Path(artifact_path)
        Path(output_file).replace(final_path)

    # Verify exported artifact integrity.
    try:
        computed_service_id = compute_id(service_id)

        if computed_service_id != service_id:
            final_path.unlink(missing_ok=True)
            raise PublisherError(
                "Integrity verification failed after export: "
                f"expected {service_id}, got {computed_service_id}."
            )

    except Exception:
        final_path.unlink(missing_ok=True)
        raise

    return service_id, final_path


class _FilePart:
    """``length`` bytes of a file, from ``offset``, as a request body.

    ``requests`` reads a body that has ``read`` and ``__len__`` in blocks and sends it
    with a Content-Length, so a part of almost 2 GiB is never held in memory.
    """

    def __init__(self, path: Path, offset: int, length: int):
        self._file = path.open("rb")
        self._file.seek(offset)
        self._remaining = length
        self._length = length

    def __len__(self) -> int:
        return self._length

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            self.close()
            return b""
        if size is None or size < 0 or size > self._remaining:
            size = self._remaining
        data = self._file.read(size)
        self._remaining -= len(data)
        if not data:
            self._remaining = 0
        return data

    def __iter__(self):
        while True:
            block = self.read(1024 * 1024)
            if not block:
                return
            yield block

    def close(self):
        if not self._file.closed:
            self._file.close()

    def __del__(self):
        self.close()


def _plan_assets(
    file_size: int,
    split_size: int,
    part_size: int,
    service_id: str,
) -> List[Tuple[str, int, int]]:
    """The assets the file becomes, as (name, offset, size).

    One ``<service_id>.celaut.bee`` asset when the file is not above ``split_size``;
    otherwise parts of ``part_size`` bytes, joined back through a manifest.
    """
    bee_name = f"{service_id}.celaut.bee"
    if file_size <= split_size:
        return [(bee_name, 0, file_size)]
    total_parts = math.ceil(file_size / part_size)
    return [
        (
            f"{bee_name}.part{index:04d}",
            index * part_size,
            min(part_size, file_size - index * part_size),
        )
        for index in range(total_parts)
    ]


def _upload_to_release(
    source_path: Path,
    provider: GitHubReleaseProvider,
    split_size: int,
    part_size: int,
    service_id: str,
) -> Dict:
    """Publish ``source_path`` as the assets of the release of ``service_id``.

    The release is a draft until every asset is uploaded, and is deleted if an upload
    fails, so a published release never points to a missing part. Publishing the same
    service again reuses its release when the assets are already there.
    """
    tag = f"{RELEASE_TAG_PREFIX}{service_id}"
    file_size = source_path.stat().st_size
    plan = _plan_assets(file_size, split_size, part_size, service_id)
    is_chunked = file_size > split_size

    expected = {name: size for name, _, size in plan}
    manifest_bytes: Optional[bytes] = None
    if is_chunked:
        manifest_lines = [provider.download_url(tag, name) for name, _, _ in plan]
        manifest_bytes = ("\n".join(manifest_lines) + "\n").encode("utf-8")
        expected[MANIFEST_ASSET_NAME] = len(manifest_bytes)
        source_url = provider.download_url(tag, MANIFEST_ASSET_NAME)
    else:
        source_url = provider.download_url(tag, plan[0][0])

    print(f"Publishing '{source_path.name}' to the release '{tag}' of {provider.repo}", flush=True)
    print(f"Service hash: {service_id} | Parts: {len(plan)}", flush=True)

    provider.check_access()
    release = provider.find_release(tag)

    def result(release_info: Dict, reused: bool) -> Dict:
        return {
            "manifest": manifest_bytes.decode("utf-8") if manifest_bytes else None,
            "manifest_url": source_url,
            "is_chunked": is_chunked,
            "release_url": release_info.get("html_url", ""),
            "tag": tag,
            "service_id": service_id,
            "total_parts": len(plan),
            "reused": reused,
        }

    if release is not None and not release.get("draft"):
        uploaded = {
            asset.get("name"): asset.get("size")
            for asset in provider.list_assets(release["id"])
            if asset.get("state", "uploaded") == "uploaded"
        }
        if uploaded == expected:
            print(f"Release '{tag}' already holds this service. Nothing to upload.", flush=True)
            return result(release, reused=True)
        print(
            f"Release '{tag}' exists but its assets do not match this upload "
            "(an incomplete publish, or other SPLIT_SIZE_MB / PART_SIZE_MB values). "
            "Replacing them.",
            flush=True,
        )
        release = provider.update_release(release["id"], draft=True)
    elif release is not None:
        print(f"Reusing the draft release '{tag}' left by an earlier run.", flush=True)
    else:
        release = provider.create_draft_release(
            tag,
            title=f"Service {service_id[:12]}",
            body=(
                f"Celaut service `{service_id}`, published by `nodo publish`.\n\n"
                f"Import it with:\n\n    nodo download {source_url}\n"
            ),
        )

    release_id = release["id"]
    try:
        for asset in provider.list_assets(release_id):
            provider.delete_asset(asset["id"])

        for index, (name, offset, size) in enumerate(plan):
            provider.upload_asset(
                release_id,
                name,
                lambda offset=offset, size=size: _FilePart(source_path, offset, size),
                size,
            )
            print(f"Uploaded part {index + 1}/{len(plan)} ({name})", flush=True)

        if manifest_bytes is not None:
            provider.upload_asset(
                release_id, MANIFEST_ASSET_NAME, lambda: manifest_bytes, len(manifest_bytes)
            )
            print("Uploaded manifest", flush=True)

        release = provider.update_release(release_id, draft=False)
    except BaseException:
        print(f"Upload failed. Deleting the incomplete release '{tag}'.", flush=True)
        try:
            provider.delete_release(release_id)
        except Exception as cleanup_exc:
            print(
                f"⚠️  Could not delete the draft release '{tag}': {cleanup_exc}. "
                "It is not public; the next nodo publish of this service reuses it.",
                flush=True,
            )
        raise

    return result(release, reused=False)


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


# Write route on a running source-application instance's REST API, confirmed against
# the deployed service (``.service/server-http.mjs`` + ``mcp/writes.mjs``): a FILE_SOURCE
# opinion is published via ``POST /api/sources`` with ``{mainBoxId, fileHash, sourceEntry}``.
# The instance signs + submits itself when it was launched in seed mode
# (``SOURCE_SIGNER_MODE=seed`` + ``SOURCE_MNEMONIC`` in its env); the mnemonic is NEVER
# sent in the request body. In unsigned mode the instance instead returns an unsigned
# EIP-12 tx (``{submitted:false,...}``) and we fall back to a manual link.
SOURCE_APPLICATION_PUBLISH_ROUTE = "api/sources"


def _build_source_signer_envs() -> Optional[Dict[str, str]]:
    """Seed-signer environment for an auto-launched source-application instance.

    Maps the node's Ergo wallet into the env the service reads (see the deployed
    service's ``mcp/lib.mjs``): ``SOURCE_SIGNER_MODE=seed`` + ``SOURCE_MNEMONIC`` so the
    instance signs + submits the source transaction itself, and ``SOURCE_NODE_URI`` for
    submission. Returns ``None`` when no wallet mnemonic is configured (can't run a seed
    signer), so the caller degrades to a manual link.
    """
    cfg = ConfigManager()
    mnemonic = str(cfg.get("ledgers.ergo.WALLET_MNEMONIC", "") or "").strip()
    if not mnemonic:
        return None
    envs = {"SOURCE_SIGNER_MODE": "seed", "SOURCE_MNEMONIC": mnemonic}
    node_url = str(cfg.get("ledgers.ergo.NODE_URL", "") or "").strip()
    if node_url:
        envs["SOURCE_NODE_URI"] = node_url
    return envs


def _ensure_seed_mode_source_application() -> Optional[str]:
    """Return a running source-application endpoint, auto-launching one in seed mode.

    For AUTO_PUBLISH_TX: reuse an already-running instance if present, otherwise launch
    the configured ``source-application`` core service with the node's seed injected into
    its environment (``_build_source_signer_envs``) so it can sign + submit autonomously.
    Fully defensive — a missing core-service id, no wallet mnemonic, or any launch failure
    yields ``None`` and the caller falls back to a manual registration link.
    """
    try:
        from src.core_services import SOURCE_APPLICATION, get_core_service_id
        from src.core_services.runtime import ensure_core_service_running

        source_application_id = get_core_service_id(SOURCE_APPLICATION)
        if not source_application_id:
            return None
        envs = _build_source_signer_envs()
        if not envs:
            return None  # no seed to run the instance as a signer
        return ensure_core_service_running(source_application_id, envs=envs)
    except Exception:
        return None


def _submit_source_via_instance_api(
    endpoint: str,
    *,
    main_box_id: str,
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
    """AUTO_PUBLISH_TX: publish the FILE_SOURCE opinion through the running
    source-application instance so it signs and submits the on-chain source
    transaction directly (no manual click-to-add step).

    Confirmed contract (``.service/server-http.mjs`` → ``mcp/writes.mjs``):
    ``POST /api/sources`` with ``{mainBoxId, fileHash, sourceEntry}``, where
    ``sourceEntry = {hashFunctionId, contentFormat, contentHash, rawFormat, urlLink,
    isChunked}``. ``mainBoxId`` is the node's reputation PROFILE box (its on-chain
    author identity). The instance signs with its *own* env-configured seed
    (``SOURCE_SIGNER_MODE=seed`` + ``SOURCE_MNEMONIC``) — the mnemonic is never sent
    here. The response is ``{submitted:true,txId}`` when signed, or
    ``{submitted:false,unsignedTransaction,...}`` when the instance runs unsigned; only
    the former counts as success. Best-effort: returns ``False`` on any failure (or an
    unsigned result) so the caller falls back to a registration link.
    """
    url = f"{endpoint.rstrip('/')}/{SOURCE_APPLICATION_PUBLISH_ROUTE}"
    source_entry = {
        "hashFunctionId": hash_function_id,
        "contentFormat": content_format,
        "contentHash": content_hash,
        "rawFormat": raw_format,
        "urlLink": manifest_url,
        "isChunked": is_chunked,
    }
    payload = {
        "mainBoxId": main_box_id,
        "fileHash": file_hash,
        "sourceEntry": source_entry,
    }

    try:
        response = _request_with_retry(
            "POST",
            url,
            headers={"Content-Type": "application/json"},
            timeout_s=timeout_s,
            max_retry=max_retry,
            backoff_s=backoff_s,
            json=payload,
        )
    except PublisherError as exc:
        print(f"⚠️  Auto source-tx submit failed: {exc}", flush=True)
        return False
    except Exception as exc:  # defensive: the auto path must never break publish
        print(f"⚠️  Auto source-tx submit errored: {exc}", flush=True)
        return False

    # The instance only actually signed + submitted when running in seed mode; an
    # unsigned instance returns {submitted:false, unsignedTransaction}. Treat only a
    # confirmed submit as success so the caller can fall back to a manual link.
    try:
        body = json.loads(response.content.decode("utf-8", errors="strict"))
    except Exception:
        # No parseable body but the POST itself succeeded — assume the instance
        # accepted it (older/leaner instances may return an empty 200).
        return True
    if isinstance(body, dict) and body.get("submitted") is False:
        tx = body.get("unsignedTransaction")
        if tx is not None:
            print(
                "↩️  source-application instance is in unsigned mode (returned an "
                "unsigned tx) — it cannot auto-submit. Launch it with "
                "SOURCE_SIGNER_MODE=seed + SOURCE_MNEMONIC to enable AUTO_PUBLISH_TX.",
                flush=True,
            )
        return False
    return True


def _print_click_to_add(
    base_url: str,
    *,
    file_hash: str,
    content_hash: str,
    hash_function_id: str,
    manifest_url: str,
    content_format: str,
    raw_format: str,
    is_chunked: bool = True,
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
        is_chunked=is_chunked,
    )
    print("Register this source in Source Application:", flush=True)
    print(f"- Source application URL: {base_url}", flush=True)
    print(f"- Source application prefilled URL: {prefilled_url}", flush=True)
    print(f"- {'Manifest' if is_chunked else 'Artifact'} URL: {manifest_url}", flush=True)
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
    is_chunked: bool = True,
) -> None:
    """Decide, across four levels, how the freshly-published source is registered.

    1. ``AUTO_PUBLISH_TX`` on **and** a configured PROFILE box
       (``publisher.SOURCE_PROFILE_BOX_ID``) → publish the FILE_SOURCE opinion through
       a source-application instance's ``POST /api/sources``. The node **auto-launches**
       the instance in seed mode (node wallet mnemonic injected into its env) if one
       isn't already running, so it signs + submits the tx itself.
    2. A running instance but auto-submit off/unavailable, and a web page is
       configured → a prefilled click-to-add link on the **web app** (the running
       instance serves only ``/api`` + ``/mcp`` JSON, never the prefill UI).
    3. No instance, but ``publisher.SOURCE_APPLICATION_WEB_PAGE`` is set → the same
       prefilled click-to-add link (the pre-existing behaviour).
    4. Nothing usable → a manual-registration message.

    The manual link (levels 2–3) always targets the configured web app, never a bare
    instance endpoint, because only the static web app renders the prefilled add form.
    """
    hash_function_id = settings["hash_spec"].id_bytes.hex()
    web_page = settings["source_application_web_page"]

    # Level 1 — auto-submit the FILE_SOURCE opinion via a seed-mode instance (launching
    # one with the node seed in its env if necessary).
    if settings["auto_publish_tx"]:
        main_box_id = str(ConfigManager().get("publisher.SOURCE_PROFILE_BOX_ID", "") or "").strip()
        if not main_box_id:
            print(
                "⚠️  AUTO_PUBLISH_TX is enabled but publisher.SOURCE_PROFILE_BOX_ID is unset — "
                "the on-chain opinion needs the node's reputation PROFILE box id as its author. "
                "Mint a profile once (source-application web app or POST /api/profile) and set "
                "its box id in config. Falling back to a registration link.",
                flush=True,
            )
        else:
            print(
                "AUTO_PUBLISH_TX enabled — ensuring a seed-mode source-application instance "
                "and publishing the source opinion (POST /api/sources)...",
                flush=True,
            )
            endpoint = _ensure_seed_mode_source_application()
            if not endpoint:
                print(
                    "⚠️  Could not start a seed-mode source-application instance "
                    "(need a configured 'source-application' core service + "
                    "ledgers.ergo.WALLET_MNEMONIC + a running gateway). Falling back to a link.",
                    flush=True,
                )
            else:
                submitted = _submit_source_via_instance_api(
                    endpoint,
                    main_box_id=main_box_id,
                    file_hash=service_id,
                    content_hash=content_hash,
                    hash_function_id=hash_function_id,
                    manifest_url=manifest_url,
                    content_format=settings["content_format"],
                    raw_format=settings["raw_format"],
                    timeout_s=settings["timeout_s"],
                    max_retry=settings["max_retry"],
                    backoff_s=settings["backoff_s"],
                    is_chunked=is_chunked,
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

    # Levels 2 & 3 — a prefilled click-to-add link on the configured web app. A running
    # instance does not change the target: only the static web app renders the add form.
    if web_page:
        _print_click_to_add(
            web_page,
            file_hash=service_id,
            content_hash=content_hash,
            hash_function_id=hash_function_id,
            manifest_url=manifest_url,
            content_format=settings["content_format"],
            raw_format=settings["raw_format"],
            is_chunked=is_chunked,
        )
        return

    # Level 4 — nothing configured: tell the user how to register manually.
    print(
        "ℹ️  Source uploaded, but no source registration path is available "
        "(publisher.SOURCE_APPLICATION_WEB_PAGE is not set). Register the source manually "
        "using the file hash, content hash and source URL above.",
        flush=True,
    )


def publish_service(
    service_ref: str
) -> Dict:
    config = ConfigManager()
    settings = _get_publisher_settings(config, require_token=True)

    provider = GitHubReleaseProvider(
        token=settings["token"],
        repo=settings["repo"],
        target=settings["branch"],
        timeout_s=settings["timeout_s"],
        max_retry=settings["max_retry"],
        backoff_s=settings["backoff_s"],
    )

    service_id, service_file_path = _export_service_to_bee(service_ref)
    content_hash = hash_file(service_file_path, settings["hash_spec"]).hex()
    try:
        result = _upload_to_release(
            source_path=service_file_path,
            provider=provider,
            split_size=settings["split_size_mb"] * 1024 * 1024,
            part_size=settings["part_size_mb"] * 1024 * 1024,
            service_id=service_id,
        )
    finally:
        if service_file_path.exists():
            service_file_path.unlink()

    url_label = "Manifest URL" if result["is_chunked"] else "Artifact URL"
    print("Publish completed successfully.", flush=True)
    print(f"Service id: {service_id}", flush=True)
    print(f"File hash: {service_id}", flush=True)
    print(f"Content hash: {content_hash}", flush=True)
    print(f"{url_label}: {result['manifest_url']}", flush=True)
    print(f"Release URL: {result['release_url']}", flush=True)
    print(f"Download command: nodo download {result['manifest_url']}", flush=True)

    # Four-level source registration (auto-tx via instance / instance link /
    # configured web page link / manual message). See _announce_source_registration.
    _announce_source_registration(
        settings,
        service_id=service_id,
        content_hash=content_hash,
        manifest_url=result["manifest_url"],
        is_chunked=result["is_chunked"],
    )
    return result


def _is_direct_bee_url(url: str) -> bool:
    """True when ``url`` points straight at a `.celaut.bee` artifact rather than
    a plain-text manifest listing one chunk URL per line."""
    return urlparse(url).path.lower().endswith(".celaut.bee")


def download_from_manifest_url(manifest_url: str, output_dir: Optional[str] = None) -> Dict:
    """Acquire a service from ``manifest_url`` and import it.

    ``manifest_url`` may be either:
      * a plain-text manifest listing one chunk URL per line (what ``nodo publish``
        gives for a file above ``publisher.SPLIT_SIZE_MB``), each chunk fetched and
        concatenated in order; or
      * a direct HTTPS link to a `.celaut.bee` artifact (path ending in
        ``.celaut.bee``, what ``nodo publish`` gives for a smaller file), downloaded
        as-is in a single request.

    GitHub Release URLs (``releases/download/<tag>/<name>``) redirect to the file;
    ``requests`` follows the redirect. Old ``raw.githubusercontent.com`` manifests work
    the same way.

    A response that is neither valid UTF-8 manifest text nor a recognizable
    `.celaut.bee` URL is still treated as raw `.celaut.bee` bytes, so a link
    missing that extension (e.g. behind a redirect) still works.
    """
    config = ConfigManager()
    settings = _get_publisher_settings(config, require_token=False)
    target_dir = Path(output_dir or settings["output_dir"]).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    headers: Dict[str, str] = {}
    if settings["token"]:
        headers["Authorization"] = f"token {settings['token']}"

    path_parts = [part for part in urlparse(manifest_url).path.split("/") if part]
    if len(path_parts) < 2:
        raise PublisherError(f"Invalid manifest URL path: {manifest_url}")

    fetched_bytes = _fetch_bytes(
        manifest_url,
        headers=headers,
        timeout_s=settings["timeout_s"],
        max_retry=settings["max_retry"],
        backoff_s=settings["backoff_s"],
    )

    chunk_urls: List[str] = []
    direct_bee_bytes: Optional[bytes] = None
    if _is_direct_bee_url(manifest_url):
        direct_bee_bytes = fetched_bytes
    else:
        try:
            manifest_text = fetched_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # Not UTF-8 manifest text either: treat the response itself as the
            # raw .celaut.bee artifact.
            direct_bee_bytes = fetched_bytes
        else:
            chunk_urls = [line.strip() for line in manifest_text.splitlines() if line.strip()]
            if not chunk_urls:
                raise PublisherError("Manifest is empty. It must contain one chunk URL per line.")

    uuid = uuid4().hex[:8]
    output_path = target_dir / f"{uuid}.celaut.bee"

    if direct_bee_bytes is not None:
        output_path.write_bytes(direct_bee_bytes)
        print(f"Downloaded '.celaut.bee' artifact directly from {manifest_url}", flush=True)
    else:
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

        else:
            answer = input(
                f"Service import failed. Delete the exported file '{output_path}'? [Y/n]: "
            ).strip().lower()

            if answer in ("", "y", "yes"):
                os.remove(output_path)
                print("Exported file deleted.")
            else:
                print(f"Exported file kept at: {output_path}")

    if not settings["keep_artifacts"] and output_path.exists():
        output_path.unlink()
        print(f"Removed downloaded artifact: {output_path}", flush=True)
    elif output_path.exists() and imported_service_id:
            final_output_path = target_dir / f"{imported_service_id}.celaut.bee"
            output_path.rename(final_output_path)
            print(f"Downloaded artifact kept at: {final_output_path}", flush=True)

    print("Download completed successfully.", flush=True)
    if imported_service_id:
        print(f"\nRun it with:\n   nodo execute {imported_service_id}\n(from another machine, e.g. over ssh, reach it with `nodo tunnel`)", flush=True)
    return {
        "manifest": chunk_urls or [manifest_url],
        "manifest_url": manifest_url,
        "service_hash": imported_service_id,
        "output_path": str(output_path),
        "service_id": imported_service_id,
    }

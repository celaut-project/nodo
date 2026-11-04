"""Auto-acquire a missing service through the ``source-application`` core service.

The source-application is the core service that maps a service id (content hash) to
the *sources* where that service can be downloaded from (the manifest URLs produced
by ``nodo publish`` — see :mod:`src.publisher.publisher`). When ``nodo execute`` is
asked to run a service the node does not have locally, this module asks the
source-application for that service's sources and downloads it, **reusing the exact
same acquisition path as ``nodo download``** (:func:`download_from_manifest_url`,
which fetches the manifest's chunks and imports them into the registry via
``import_bee``). Nothing here re-implements downloading or storage.

Before the source-application, :func:`acquire_service` asks the known peers for the
service (``GetService``, the same path as ``nodo get --now``). The source-application
is only used when no peer provides it.

Trust / fail-closed: the fallback is only attempted when a non-placeholder
``source-application`` id is present in ``core_services`` (see
:func:`src.core_services.get_core_service_id`). If it is unset, or the lookup/download
fails, the caller falls back to the existing "Service not allowed." behaviour. The node
never downloads from an arbitrary, unconfigured source.

``acquire_service`` is for the operator's own commands and trusts the sources as ``nodo
download`` does. :func:`acquire_from_sources` with a limit is the same lookup for the
paths that a service or a peer can trigger (``maintain.check_wanted_service``, when
``service_sources.AUTO_ACQUIRE`` is on): the download is capped and guarded
(:mod:`src.core_services.source_download`) and what is imported must be the id that was asked.
"""

import json
import os
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import quote

from src.core_services import SOURCE_APPLICATION, get_core_service_id
from src.core_services.runtime import ensure_core_service_running
from src.core_services.source_download import SourceDownloadError, download_to_file
from src.commands.publisher.publisher import (
    PublisherError,
    _fetch_bytes,
    download_from_manifest_url,
)
from src.utils.config import ConfigManager

_env_manager = ConfigManager()

# When each service id was last looked up by `acquire_from_sources_if_enabled`, so that a
# service that no source has is not asked for again at every pass of the manager.
_last_attempt: Dict[str, float] = {}

# Set while the source-application itself is being made available: it cannot be asked for
# its own sources, so a lookup that starts in that state ends at once.
_lookup_state = threading.local()


def _parse_sources(payload: bytes) -> List[str]:
    """Extract manifest URLs from a source-application lookup response.

    Accepts (in order of preference):
      * JSON list of strings: ``["https://.../manifest", ...]``
      * JSON list of objects with a ``manifest_url`` / ``urlLink`` / ``url`` field
      * JSON object with a ``sources`` list of either of the above
      * newline-delimited plain text, one manifest URL per line
    """
    text = payload.decode("utf-8", errors="strict").strip()
    if not text:
        return []

    def _from_items(items) -> List[str]:
        urls: List[str] = []
        for item in items:
            if isinstance(item, str):
                url = item.strip()
            elif isinstance(item, dict):
                url = str(
                    item.get("manifest_url")
                    or item.get("urlLink")
                    or item.get("url")
                    or ""
                ).strip()
            else:
                url = ""
            if url:
                urls.append(url)
        return urls

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip()]

    if isinstance(data, dict):
        data = data.get("sources", [])
    if isinstance(data, list):
        return _from_items(data)
    return []


def __lookup_sources(service_id: str, source_application_id: Optional[str] = None) -> List[str]:
    """Query the source-application for the manifest URLs of ``service_id``.

    Returns a list of manifest URLs (possibly empty). This is the single integration
    seam with the published source-application service. The read route is confirmed
    against the deployed service (``.service/server-http.mjs``): a running instance
    exposes a JSON REST mirror under ``/api/*`` on port 8080; sources for a file are
    fetched via ``GET /api/sources?hash=<fileHash>`` (``fetchFileSourcesByHash``). The
    static web app (``SOURCE_APPLICATION_WEB_PAGE``) has no server API, so reads require
    a *running* on-node instance — not the web page. All HTTP I/O reuses the publisher's
    retrying fetch helper rather than introducing a new HTTP client.

    ``source_application_id`` is the id of the core service (``core_services``), the one
    that is made available; ``service_id`` is only what is looked up.
    """
    if getattr(_lookup_state, "active", False):
        return []
    source_application_id = source_application_id or get_core_service_id(SOURCE_APPLICATION)
    if not source_application_id:
        return []
    _lookup_state.active = True
    try:
        endpoint = ensure_core_service_running(source_application_id)
    finally:
        _lookup_state.active = False
    if not endpoint:
        # No running instance to answer /api reads (the static web app cannot).
        print(f"ℹ️  No running instance of Source Application service to answer /api reads.")
        return []
    url = f"{endpoint.rstrip('/')}/api/sources?hash={quote(service_id, safe='')}"
    try:
        payload = _fetch_bytes(url)
    except PublisherError:
        return []
    return _parse_sources(payload)


def _acquire_from_peers(service_id: str) -> bool:
    """Ask the known peers for ``service_id`` (their ``GetService``), as ``nodo get --now`` does."""
    try:
        bytes.fromhex(service_id)
    except ValueError:
        # A tag nobody resolved: peers are asked by hash only.
        return False

    # Imported here: the manager module pulls in the whole node (DB, payments, ...).
    from src.manager.maintain import fetch_service_from_peers

    print(f"🔎 Asking known peers for '{service_id}'...")
    try:
        if fetch_service_from_peers(service_id):
            print("✅ Service acquired from a peer.")
            return True
    except Exception as exc:  # defensive: a peer failure must not break execute
        print(f"⚠️  Peer lookup failed: {exc}")
        return False
    print("ℹ️  No known peer provided the service.")
    return False


def acquire_service(service_id: str) -> bool:
    """Best-effort: get ``service_id`` from a peer, else via the source-application.

    Peers are asked first: a peer that holds the service sends it over the node
    network, so the source-application (and the download from its sources) is only
    needed for a service no known peer has.

    Returns ``True`` only if the service was successfully downloaded AND imported into
    the local registry (so the caller can re-resolve and execute it). Returns ``False``
    if no peer has it and the source-application is not configured, has no source for
    the service, or every candidate source fails to download — never raising into the
    execute path.
    """
    if _acquire_from_peers(service_id):
        return True
    return acquire_from_sources(service_id)


def acquire_from_sources(service_id: str, max_bytes: Optional[int] = None) -> bool:
    """Get ``service_id`` from the sources that the source-application lists for it.

    ``max_bytes`` ``None`` is the operator's path (``nodo execute`` and the like): the
    source is downloaded as ``nodo download`` does, with no limit. An integer is the path
    of a caller that is not the operator: the download is capped and guarded
    (:mod:`src.core_services.source_download`), ``0`` meaning no cap, and it is refused
    unless ``VALIDATE_ON_IMPORT`` is on, because otherwise the id of what is imported would
    be the one that the file says and not the one that was asked for.

    True only when the service is in the registry afterwards. Never raises.
    """
    source_application_id = get_core_service_id(SOURCE_APPLICATION)
    if not source_application_id:
        print(
            "ℹ️  Skipping auto-download: no 'source-application' core service configured. "
            "Set its id under 'core_services' in config.yaml to enable resolving missing services."
        )
        return False

    if max_bytes is not None and not _validates_on_import():
        print(
            "ℹ️  Skipping auto-download: it needs 'misc.VALIDATE_ON_IMPORT' on, so that what is "
            "imported is checked against the id that was asked for."
        )
        return False

    print(f"🔎 Looking up '{service_id}' via the source-application core service...")
    try:
        sources = __lookup_sources(service_id, source_application_id)
    except Exception as exc:  # defensive: a lookup failure must not break execute
        print(f"⚠️  source-application lookup failed: {exc}")
        return False

    if not sources:
        print("⚠️  source-application returned no sources for this service.")
        return False

    for manifest_url in sources:
        try:
            print(f"⬇️  Downloading service from source: {manifest_url}")
            if max_bytes is None:
                result = download_from_manifest_url(manifest_url)
                acquired = bool(result.get("service_id"))
            else:
                acquired = _download_checked(manifest_url, service_id, max_bytes)
        except (PublisherError, SourceDownloadError) as exc:
            print(f"⚠️  Source failed ({manifest_url}): {exc}")
            continue
        except Exception as exc:  # defensive: try the next source
            print(f"⚠️  Unexpected error downloading from {manifest_url}: {exc}")
            continue

        if acquired:
            print("✅ Service acquired via source-application.")
            return True

    print("⚠️  All source-application sources failed to provide the service.")
    return False


def _validates_on_import() -> bool:
    from src.commands import import_bee as import_module

    return bool(import_module.VALIDATE_ON_IMPORT)


def _download_checked(manifest_url: str, service_id: str, max_bytes: int) -> bool:
    """Download, import and check one source. True when ``service_id`` is imported.

    The file goes to a temporary file under the cache and is always removed. Whatever the
    import added to the registry that is not ``service_id`` is removed again: the source
    gave another service, which nobody asked for.
    """
    from uuid import uuid4

    from src.commands import import_bee as import_module
    from src.commands.remove import remove

    timeout_s = float(_env_manager.get("service_sources.TIMEOUT_SECONDS", 60) or 60)
    directory = os.path.join(str(_env_manager.get("CACHE")), "source_downloads")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{uuid4().hex}.celaut.bee")
    try:
        size = download_to_file(manifest_url, path, max_bytes, timeout_s)
        print(f"Downloaded {size} bytes.")
        before = set(os.listdir(import_module.REGISTRY))
        imported = import_module.import_bee(path)
        if imported == service_id:
            return True
        # Another service, or none: take away what this import added.
        for added in set(os.listdir(import_module.REGISTRY)) - before:
            if added != service_id:
                print(f"⚠️  The source gave {added}, not {service_id}: removing it.")
                remove(added)
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def acquire_from_sources_if_enabled(service_id: str) -> bool:
    """The automatic path of ``maintain.check_wanted_service``: the sources, if the node
    allows it (``service_sources.AUTO_ACQUIRE``), with its limit and its cooldown.

    False when it is off, when ``service_id`` was tried less than ``RETRY_AFTER_SECONDS``
    ago, or when the service was not acquired. Never raises.
    """
    try:
        if not bool(_env_manager.get("service_sources.AUTO_ACQUIRE", False)):
            return False
        retry_after = float(_env_manager.get("service_sources.RETRY_AFTER_SECONDS", 3600) or 0)
        max_bytes = int(_env_manager.get("service_sources.MAX_DOWNLOAD_MB", 2048) or 0) * 1024 * 1024
        if max_bytes < 0 or retry_after < 0:
            print("⚠️  service_sources: a negative value is not valid; the sources are not asked.")
            return False
        now = time.monotonic()
        last = _last_attempt.get(service_id)
        if last is not None and now - last < retry_after:
            return False
        _last_attempt[service_id] = now
        return acquire_from_sources(service_id, max_bytes)
    except Exception as exc:  # the manager loop must go on
        print(f"⚠️  Auto-acquire of {service_id} failed: {exc}")
        return False

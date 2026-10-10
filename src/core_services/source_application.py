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
"""

import json
from typing import List
from urllib.parse import quote

from src.core_services import SOURCE_APPLICATION, get_core_service_id
from src.core_services.runtime import ensure_core_service_running
from src.commands.publisher.publisher import (
    PublisherError,
    _fetch_bytes,
    download_from_manifest_url,
)


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


def __lookup_sources(service_id: str) -> List[str]:
    """Query the source-application for the manifest URLs of ``service_id``.

    Returns a list of manifest URLs (possibly empty). This is the single integration
    seam with the published source-application service. The read route is confirmed
    against the deployed service (``.service/server-http.mjs``): a running instance
    exposes a JSON REST mirror under ``/api/*`` on port 8080; sources for a file are
    fetched via ``GET /api/sources?hash=<fileHash>`` (``fetchFileSourcesByHash``). The
    static web app (``SOURCE_APPLICATION_WEB_PAGE``) has no server API, so reads require
    a *running* on-node instance — not the web page. All HTTP I/O reuses the publisher's
    retrying fetch helper rather than introducing a new HTTP client.
    """
    endpoint = ensure_core_service_running(service_id)
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

    source_application_id = get_core_service_id(SOURCE_APPLICATION)
    if not source_application_id:
        print(
            "ℹ️  Skipping auto-download: no 'source-application' core service configured. "
            "Set its id under 'core_services' in config.yaml to enable resolving missing services."
        )
        return False

    print(f"🔎 Looking up '{service_id}' via the source-application core service...")
    try:
        sources = __lookup_sources(service_id)
    except Exception as exc:  # defensive: a lookup failure must not break execute
        print(f"⚠️  source-application lookup failed: {exc}")
        return False

    if not sources:
        print("⚠️  source-application returned no sources for this service.")
        return False

    for manifest_url in sources:
        try:
            print(f"⬇️  Downloading service from source: {manifest_url}")
            result = download_from_manifest_url(manifest_url)
        except PublisherError as exc:
            print(f"⚠️  Source failed ({manifest_url}): {exc}")
            continue
        except Exception as exc:  # defensive: try the next source
            print(f"⚠️  Unexpected error downloading from {manifest_url}: {exc}")
            continue

        if result.get("service_id"):
            print("✅ Service acquired via source-application.")
            return True

    print("⚠️  All source-application sources failed to provide the service.")
    return False

"""
`nodo pack` — packer-service HTTP client.

nodo no longer builds services locally with Docker. Packing is delegated to a
**packer service** (https://github.com/agenticaihome/celaut-packer-service): a
Celaut microVM that runs Docker/buildx *inside* a sealed VM and exposes an HTTP
`/pack` endpoint. This keeps Docker entirely out of the nodo host.

Flow:
  1. prepare_directory  — resolve the project (local path or git URL).   [Docker-free, client-side]
  2. resolve_and_upload_dependencies — push registry-hash dependencies to
     the packer (its own registry starts empty).                        [client-side]
  3. generate_service_zip — build the `.service.zip` archive.            [Docker-free, client-side]
  4. POST the zip to  <PACKER_SERVICE_URL>/pack  → returns the packed
     `.celaut.bee` body + `X-Service-Id` header.                         [build happens in the packer VM]
  5. import_bee        — import the `.bee` into this node's REGISTRY /
     METADATA_REGISTRY (reuses nodo's existing import logic).

Configuring the packer (resolution order):
  1. service id   PACKER_SERVICE_ID  (env var, config `packer.PACKER_SERVICE_ID`,
     or a `{name: "packer", id: ...}` entry in the top-level `core_services` list)
     — the published content hash of the packer-service. nodo treats the packer as
     a **core service**: if no instance is already running, it downloads (via the
     source-application) and launches it on demand through the core-services runtime
     (:func:`src.core_services.runtime.ensure_core_service_running`), then packs
     against the live instance's `ip:port`.
  2. url override  PACKER_SERVICE_URL (env var or config packer.PACKER_SERVICE_URL)
     — only needed to point at an out-of-band packer (one running elsewhere, not as
     a local instance). Used as a last resort when no service id is set, or when a
     configured id can neither be found running nor be launched.
If neither yields an endpoint, `nodo pack` fails with an actionable message
instead of trying to use a local Docker that no longer exists.
"""
import os
import sys
import sqlite3
import tempfile
from typing import Optional

import requests

from src.commands.packer.zip_with_dockerfile.prepare_directory import prepare_directory
from src.commands.packer.zip_with_dockerfile.generate_service_zip import generate_service_zip
from src.utils.config import ConfigManager

# `import_bee` pulls in the bee_rpc runtime (only needed when actually importing a
# packed .bee). Imported lazily inside pack() so endpoint resolution / config
# helpers don't require the full runtime stack.

env_manager = ConfigManager()

GATEWAY_PORT = env_manager.get("GATEWAY_PORT")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")
# Optional out-of-band packer override (an already-running packer reachable at a
# fixed URL). Empty -> resolve the packer by service id via the core-services
# runtime instead.
PACKER_SERVICE_URL = env_manager.get("PACKER_SERVICE_URL") or ""
# Connect timeout is short; there is NO read timeout because a real build can
# take many minutes and the server holds the connection open until it finishes.
_CONNECT_TIMEOUT = 30


def _resolve_packer_id() -> Optional[str]:
    """Resolve the packer-service id (content hash).

    Resolution order: ``PACKER_SERVICE_ID`` env var → config ``packer.PACKER_SERVICE_ID``
    → a ``{name: "packer", id: ...}`` entry in the unified top-level ``core_services``
    list. The last one keeps the packer consistent with every other core service the
    node bootstraps (source-application, low-demand-fallback, ...).
    """
    service_id = os.environ.get("PACKER_SERVICE_ID") or env_manager.get("packer.PACKER_SERVICE_ID")
    if isinstance(service_id, str):
        service_id = service_id.strip()
    if not service_id:
        # Fall back to the unified core_services list (never fabricates an id:
        # returns None for a missing/placeholder entry).
        try:
            from src.core_services import PACKER, get_core_service_id
            service_id = get_core_service_id(PACKER)
        except Exception:
            service_id = None
    return service_id or None


def _resolve_packer_url() -> Optional[str]:
    """Resolve the packer-service base URL override: env var first, then config."""
    url = os.environ.get("PACKER_SERVICE_URL") or env_manager.get("packer.PACKER_SERVICE_URL")
    if isinstance(url, str):
        url = url.strip()
    return url.rstrip("/") if url else None


def _resolve_packer_endpoint_by_id(service_id: str) -> Optional[str]:
    """Find a running local instance of `service_id` and return its `http://ip:port`.

    Looks up `local_instances` in the node's sqlite DATABASE_FILE, parses each
    matching row's serialized `celaut.Instance` protobuf, and returns the first
    `http://ip:port` it can build from `uri_slot[*].uri[*]`. Fully defensive: any
    problem (no DB, no table, no rows, parse error, no uri) returns None.
    """
    if not service_id:
        return None

    # Imported lazily/inside the guard so a missing protobuf module can't crash
    # the whole resolution path.
    try:
        from protos import celaut_pb2 as celaut
    except Exception:
        return None

    conn = None
    try:
        database_file = env_manager.get("DATABASE_FILE")
        if not database_file:
            return None
        conn = sqlite3.connect(database_file)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT serialized_instance FROM local_instances WHERE service_id = ?",
            (service_id,),
        )
        rows = cursor.fetchall()
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    for row in rows:
        serialized = row[0] if row else None
        if not serialized:
            continue
        try:
            inst = celaut.Instance()
            inst.ParseFromString(serialized)
            for _slot in inst.uri_slot:
                for _uri in _slot.uri:
                    ip = str(_uri.ip).strip()
                    port = _uri.port
                    if ip and port:
                        return f"http://{ip}:{port}"
        except Exception:
            continue

    return None


def _ensure_packer_running(service_id: str) -> Optional[str]:
    """Download+launch the packer service by id and return its live endpoint.

    Delegates to the core-services runtime, which mirrors how every other core
    service is bootstrapped: return an already-running instance, else acquire the
    service via the source-application, else launch it through the existing
    ``nodo execute`` path — then resolve its ``http://ip:port``. Fully defensive:
    any failure (no runtime deps, no gateway, launch error) degrades to ``None`` so
    the caller can fall back to the URL override.
    """
    if not service_id:
        return None
    try:
        from src.core_services.runtime import ensure_core_service_running
        return ensure_core_service_running(service_id)
    except Exception:
        return None


def _no_packer_configured_message() -> str:
    return (
        "\nNo packer service is configured.\n\n"
        "nodo does not build services locally anymore — packing is done by a\n"
        "packer-service microVM (it runs Docker/buildx inside a sealed VM, so you\n"
        "never install Docker on this host).\n\n"
        "Configure the packer by its published service id and re-run `nodo pack`;\n"
        "nodo will download and launch a packer instance on demand and pack\n"
        "against it:\n"
        "  • config.yaml (either location works):\n"
        "        packer:\n"
        "          PACKER_SERVICE_ID: \"<packer-service published id>\"\n"
        "    or, alongside the other core services:\n"
        "        core_services:\n"
        "          - name: \"packer\"\n"
        "            id: \"<packer-service published id>\"\n"
        "    (or env var:  export PACKER_SERVICE_ID=<packer-service published id>)\n\n"
        "To point at an out-of-band packer instead (one already running elsewhere),\n"
        "set the override URL:  export PACKER_SERVICE_URL=http://<ip>:8080  (or\n"
        "packer.PACKER_SERVICE_URL).\n"
    )


def __remove_path(path):
    import shutil
    if os.path.exists(path):
        (os.remove if os.path.isfile(path) else shutil.rmtree)(path)
        print(f"Removed: '{path}'")


def _resolve_packer_endpoint() -> Optional[str]:
    """Resolve the packer endpoint.

    Order:
      1. If a packer service id is configured, prefer an instance that is already
         running (fast path, no launch).
      2. Otherwise treat the packer as a core service and download+launch it on
         demand through the core-services runtime, then use the live instance.
      3. Only if neither yields an endpoint, fall back to the PACKER_SERVICE_URL
         override (an out-of-band packer).
    """
    service_id = _resolve_packer_id()
    if service_id:
        endpoint = _resolve_packer_endpoint_by_id(service_id)
        if endpoint:
            return endpoint
        # No running instance — execute the packer service (download + launch) and
        # pack against it, instead of silently falling back to a URL.
        print(
            f"No running instance of packer service id {service_id} found; "
            "downloading/launching it via the core-services runtime..."
        )
        endpoint = _ensure_packer_running(service_id)
        if endpoint:
            return endpoint
        print(
            f"Could not start packer service id {service_id}; "
            "falling back to PACKER_SERVICE_URL if set."
        )
    return _resolve_packer_url()


def pack(directory: str) -> Optional[str]:
    packer_url = _resolve_packer_endpoint()
    if not packer_url:
        print(_no_packer_configured_message())
        return None

    _id: Optional[str] = None
    # TODO Better approach, generator: return only path and finally remove if remote.
    is_remote, directory = prepare_directory(directory)

    bee_path: Optional[str] = None
    try:
        # The packer service builds inside its own sealed VM, so its registry is
        # empty. Any registry-hash dependency this project declares must be pushed
        # there first. Resolve each dependency against THIS nodo's registry (raising
        # a clear error if one is missing) and upload the registry-hash ones.
        from src.commands.packer.zip_with_dockerfile.packer_service_client import (
            resolve_and_upload_dependencies,
        )
        print(f"Resolving dependencies against packer service {packer_url} ...")
        summary = resolve_and_upload_dependencies(
            project_directory=directory,
            packer_service_url=packer_url,
        )
        if summary["uploaded"]:
            print(f"Uploaded dependencies: {', '.join(summary['uploaded'])}")
        if summary["already_present"]:
            print(f"Dependencies already on packer: {', '.join(summary['already_present'])}")

        service_zip_dir: str = generate_service_zip(project_directory=directory)

        pack_endpoint = f"{packer_url}/pack"
        print(f"Sending your project to the packer service at {pack_endpoint} ...")
        print("Building inside the packer microVM — this might take a while.")

        with open(service_zip_dir, "rb") as zip_file:
            response = requests.post(
                pack_endpoint,
                data=zip_file,
                headers={"Content-Type": "application/zip"},
                timeout=(_CONNECT_TIMEOUT, None),
            )

        if response.status_code != 200:
            print(
                f"\nPacker service returned an error (HTTP {response.status_code}):\n"
                f"{response.text}"
            )
            return None

        service_id_header = response.headers.get("X-Service-Id")
        if not response.content:
            print("\nPacker service returned an empty body; no service was produced.")
            return None

        # Persist the returned `.celaut.bee` and import it through nodo's own
        # import path (validates the hash and saves to REGISTRY/METADATA_REGISTRY).
        fd, bee_path = tempfile.mkstemp(
            suffix=".celaut.bee",
            prefix=f"{service_id_header or 'service'}_",
        )
        with os.fdopen(fd, "wb") as f:
            f.write(response.content)

        print("Compilation complete.")
        if service_id_header:
            print("Service ID -> ", service_id_header)
        print("\nImporting the packed service into the local registry...")

        from src.commands.import_bee import import_bee
        _id = import_bee(path=bee_path)

        if not _id:
            _msg = f"Failed to import the packed service for {directory}."
            print(_msg)
            raise Exception(_msg)

    except requests.exceptions.ConnectionError as e:
        print(
            f"\nCould not reach the packer service at {packer_url}: {e}\n"
            "Check packer.PACKER_SERVICE_ID (and that its instance is running via "
            "`nodo execute`) or the PACKER_SERVICE_URL override, and that the "
            "packer-service instance is running and reachable."
        )
        return None
    except Exception as e:
        print(f"Exception packing {directory}: {e}")
        return None

    finally:
        if bee_path and os.path.exists(bee_path):
            os.remove(bee_path)
        if is_remote:
            __remove_path(directory)

    return _id

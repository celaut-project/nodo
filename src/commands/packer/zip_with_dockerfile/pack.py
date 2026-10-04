"""
`nodo pack` — packer-service HTTP client.

nodo no longer builds services locally with Docker. Packing is delegated to a
**packer service** (eg: https://github.com/celaut-basics/packer-service): a
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
  1. service id   the `packer:` entry in the top-level
     `core_services` mapping — the single source of truth for the packer id (there is
     no env var and no `packer.*` id key). It is the published content hash of the
     packer-service. nodo treats the packer as a **core service**: if no instance is
     already running, it downloads and launches it on demand through the
     core-services runtime, then packs against the live instance's `ip:port`.
     Download source: when `packer.PACKER_SOURCE_URL` is set, nodo fetches the
     packer directly from that manifest URL; when it is empty, nodo resolves the
     sources via the source-application core service.
  2. url override  PACKER_SERVICE_URL (config packer.PACKER_SERVICE_URL)
     — only needed to point at an out-of-band packer (one running elsewhere, not as
     a local instance). Used as a last resort when no service id is set, or when a
     configured id can neither be found running nor be launched.
If neither yields an endpoint, or the packer does not answer, `nodo pack` asks
the operator if it must enable the local packer (``packer.local: true``) and
continue with it. Without a terminal, it fails with an actionable message.
"""
import os
import sys
import tempfile
from typing import Optional

import requests

from src.commands.packer.zip_with_dockerfile.prepare_directory import prepare_directory
from src.commands.packer.zip_with_dockerfile.generate_service_zip import generate_service_zip
from src.core_services import PACKER, get_core_service_id
from src.core_services.runtime import ensure_core_service_running
from src.utils.config import ConfigManager

# `import_bee` pulls in the bee_rpc runtime (only needed when actually importing a
# packed .bee). Imported lazily inside pack() so endpoint resolution / config
# helpers don't require the full runtime stack.

env_manager = ConfigManager()

METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")
REGISTRY = env_manager.get("REGISTRY")
# Optional out-of-band packer override (an already-running packer reachable at a
# fixed URL). Empty -> resolve the packer by service id via the core-services
# runtime instead.
PACKER_SERVICE_URL = env_manager.get("PACKER_SERVICE_URL")
# Optional direct source for downloading the packer service. When set, it is a
# manifest URL nodo fetches the packer from directly (bypassing the
# source-application lookup) before launching it as a core service. When empty,
# nodo resolves the packer's sources via the source-application core service. See
# `packer.PACKER_SOURCE_URL` in config.example.yaml.
PACKER_SOURCE_URL = env_manager.get("PACKER_SOURCE_URL")
# Connect timeout is short; there is NO read timeout because a real build can
# take many minutes and the server holds the connection open until it finishes.
_CONNECT_TIMEOUT = 30
# A freshly-launched packer resolves an endpoint the moment its VM network is up,
# but the in-VM dockerd/buildx and the packer's own HTTP server take longer to
# start serving. POSTing before that races startup and fails (connection
# refused/reset -> "Dependency packing error"). Poll GET /health until the packer
# answers 200 before sending anything. Only costs time on a cold launch; an
# already-running packer answers immediately. Override the ceiling with
# PACKER_HEALTH_TIMEOUT (seconds).
_HEALTH_TIMEOUT = int(env_manager.get("PACKER_HEALTH_TIMEOUT") or 300)
_HEALTH_POLL_INTERVAL = 3


def _resolve_packer_id() -> Optional[str]:
    """Resolve the packer-service id (content hash).

    Single source of truth: the ``packer`` entry in the unified top-level
    ``core_services`` mapping, keeping the packer consistent with every other
    core service the node bootstraps (source-application, low-demand-fallback, ...).
    There is no environment-variable or ``packer.*`` override — the id lives only in
    ``core_services``.
    """
    return get_core_service_id(PACKER) or None


def _wait_for_packer_health(packer_url: str, timeout: int = _HEALTH_TIMEOUT) -> bool:
    """Block until the packer answers ``GET /health`` with 200.

    Returns True as soon as the packer is serving, False if ``timeout`` seconds
    elapse without a healthy response. Never raises — connection errors while the
    packer VM is still booting are expected and simply retried.
    """
    import time

    health_endpoint = f"{packer_url}/health"
    deadline = time.monotonic() + max(0, timeout)
    announced = False
    while True:
        try:
            resp = requests.get(health_endpoint, timeout=(5, 10))
            if resp.status_code == 200:
                if announced:
                    print("Packer service is up.")
                return True
        except requests.exceptions.RequestException:
            pass  # not serving yet — keep polling until the deadline
        if time.monotonic() >= deadline:
            return False
        if not announced:
            print(
                f"Waiting for the packer service to start serving at {health_endpoint} "
                f"(up to {timeout}s; Docker/buildx inside the packer VM can take a "
                "minute or two on a cold launch)..."
            )
            announced = True
        time.sleep(_HEALTH_POLL_INTERVAL)


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
         demand through the core-services runtime, then use the live instance. The
         download source is PACKER_SOURCE_URL when set (fetched directly), otherwise
         the source-application core service.
      3. Only if neither yields an endpoint, fall back to the PACKER_SERVICE_URL
         override (an out-of-band packer).
    """
    service_id = _resolve_packer_id()
    if service_id:
        source_url = PACKER_SOURCE_URL.strip() if PACKER_SOURCE_URL else None
        endpoint = ensure_core_service_running(service_id, source_url=source_url)
        if endpoint:
            return endpoint
        
    if PACKER_SERVICE_URL and PACKER_SERVICE_URL.strip():
        print(
            f"Could not start packer service id {service_id}; "
            "falling back to PACKER_SERVICE_URL if set."
        )
        return PACKER_SERVICE_URL
    
    return None


# True when `nodo pack --local` selects the local packer for this run only. It
# is a module flag, so the nested packs of dependencies also build locally.
_local_for_this_run = False


def _local_packer_enabled() -> bool:
    """True when `--local` or config selects nodo's optional local packer.

    ``packer.local`` defaults to False: the node keeps the packer-service
    behaviour (resolve the packer by its ``core_services`` id, falling back to the
    ``PACKER_SERVICE_URL`` override). When True, `nodo pack` builds the service
    locally with nodo's isolated Docker toolchain instead.
    """
    return _local_for_this_run or bool(env_manager.get("packer.local", False))


# Return value of _pack_via_service() when no packer service is available. It
# is different from None (a packer that is available but did not pack), because
# only this case lets the operator change to the local packer.
_SERVICE_UNAVAILABLE = object()


def _offer_local_packer() -> bool:
    """Ask the operator to enable the local packer. Return True if enabled.

    The local packer installs nodo's rootless BuildKit toolchain (buildkitd and
    buildctl) on demand. If the answer is yes, this function writes
    ``packer.local: true`` to config.yaml, so the next packs also use it.
    Without a terminal, there is no question and the function returns False.
    """
    if not (sys.stdin is not None and sys.stdin.isatty()):
        print(
            "To build on this host instead, run `nodo pack <project dir> --local` "
            "(this run only), or set `packer.local: true` in config.yaml."
        )
        return False

    try:
        answer = input(
            "\nThe packer service is not available. Enable the local packer "
            "(packer.local: true) and continue?\n"
            "If BuildKit is not installed, nodo installs it now (this can ask for sudo). [y/N]: "
        ).strip().lower()
    except EOFError:
        return False

    if answer not in ("y", "yes"):
        return False

    env_manager.set("packer.local", True)
    print("Set packer.local: true in config.yaml.")
    return True


def _pack_local(directory: str) -> Optional[str]:
    from src.commands.packer.zip_with_dockerfile.local_pack import pack_local
    return pack_local(directory)


def pack(directory: str, local: bool = False) -> Optional[str]:
    """Pack a project into a Celaut service.

    Dispatches to the local BuildKit packer when ``local`` is True (`--local`) or
    ``packer.local: true``. Otherwise it uses the packer-service HTTP client (the
    default). If the packer service is not available, the operator can enable
    the local packer and continue.
    """
    global _local_for_this_run
    if local:
        _local_for_this_run = True
    if _local_packer_enabled():
        return _pack_local(directory)

    result = _pack_via_service(directory)
    if result is _SERVICE_UNAVAILABLE:
        if _offer_local_packer():
            return _pack_local(directory)
        return None
    return result


def _pack_via_service(directory: str):
    """Pack with the packer service.

    Returns the service id, None if the packer did not pack the project, or
    ``_SERVICE_UNAVAILABLE`` if no packer service is available.
    """
    packer_url = _resolve_packer_endpoint()
    if not packer_url:
        _msg = (
            "\nNo packer service is configured.\n\n"
            "By default, nodo does not build services locally — packing is done by a\n"
            "packer-service microVM (it runs Docker/buildx inside a sealed VM, so you\n"
            "never install Docker on this host).\n\n"
            "Configure the packer by its published service id and re-run `nodo pack`;\n"
            "nodo will download and launch a packer instance on demand and pack\n"
            "against it:\n"
            "  • config.yaml, alongside the other core services (the single source\n"
            "    of truth for the packer id):\n"
            "        core_services:\n"
            "          packer: \"<packer-service published id>\"\n\n"
            "To point at an out-of-band packer instead (one already running elsewhere),\n"
            "set the override URL in config.yaml:  packer.PACKER_SERVICE_URL: \"http://<ip>:8080\".\n"
        )
        print(_msg)
        return _SERVICE_UNAVAILABLE

    _id: Optional[str] = None
    # TODO Better approach, generator: return only path and finally remove if remote.
    is_remote, directory = prepare_directory(directory)

    # If nodo just launched the packer, its endpoint resolves before the in-VM
    # HTTP server is serving. Wait for /health before the first request (the
    # dependency upload below is the first packer contact) so we don't race
    # startup. prepare_directory() above already gave the VM some warmup time.
    if not _wait_for_packer_health(packer_url):
        print(
            f"\nThe packer service at {packer_url} did not become healthy within "
            f"{_HEALTH_TIMEOUT}s.\n"
            "It may still be starting (Docker/buildx inside the packer VM can take a\n"
            "minute or two on a cold launch). Re-run `nodo pack`, or raise the wait\n"
            "with PACKER_HEALTH_TIMEOUT (seconds)."
        )
        if is_remote:
            __remove_path(directory)
        return _SERVICE_UNAVAILABLE

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
            "Check the packer id in core_services (and that its instance is running "
            "via `nodo execute`) or the PACKER_SERVICE_URL override, and that the "
            "packer-service instance is running and reachable."
        )
        return _SERVICE_UNAVAILABLE
    except Exception as e:
        print(f"Exception packing {directory}: {e}")
        return None

    finally:
        if bee_path and os.path.exists(bee_path):
            os.remove(bee_path)
        if is_remote:
            __remove_path(directory)

    return _id

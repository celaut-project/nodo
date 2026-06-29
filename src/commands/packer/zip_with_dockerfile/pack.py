"""
`nodo pack` — packer-service HTTP client.

nodo no longer builds services locally with Docker. Packing is delegated to a
**packer service** (https://github.com/agenticaihome/celaut-packer-service): a
Celaut microVM that runs Docker/buildx *inside* a sealed VM and exposes an HTTP
`/pack` endpoint. This keeps Docker entirely out of the nodo host.

Flow:
  1. prepare_directory  — resolve the project (local path or git URL).   [Docker-free, client-side]
  2. generate_service_zip — build the `.service.zip` archive.            [Docker-free, client-side]
  3. POST the zip to  <PACKER_SERVICE_URL>/pack  → returns the packed
     `.celaut.bee` body + `X-Service-Id` header.                         [build happens in the packer VM]
  4. import_bee        — import the `.bee` into this node's REGISTRY /
     METADATA_REGISTRY (reuses nodo's existing import logic).

Configuring the packer endpoint (first match wins):
  * env var   PACKER_SERVICE_URL                 (e.g. http://10.0.0.5:8080)
  * config    packer.PACKER_SERVICE_URL  in config.yaml
The URL is the `ip:port` of a running packer-service instance (its API listens
on :8080). Point nodo at one you run yourself, or at a shared/community packer.
If unset, `nodo pack` fails with an actionable message instead of trying to use
a local Docker that no longer exists.
"""
import os
import sys
import tempfile
from typing import Optional

import requests

from src.commands.packer.zip_with_dockerfile.prepare_directory import prepare_directory
from src.commands.packer.zip_with_dockerfile.generate_service_zip import generate_service_zip
from src.commands.import_bee import import_bee
from src.utils.config import ConfigManager

env_manager = ConfigManager()

# Connect timeout is short; there is NO read timeout because a real build can
# take many minutes and the server holds the connection open until it finishes.
_CONNECT_TIMEOUT = 30


def _resolve_packer_url() -> Optional[str]:
    """Resolve the packer-service base URL: env var first, then config."""
    url = os.environ.get("PACKER_SERVICE_URL") or env_manager.get("packer.PACKER_SERVICE_URL")
    if isinstance(url, str):
        url = url.strip()
    return url.rstrip("/") if url else None


def _no_packer_configured_message() -> str:
    return (
        "\nNo packer service is configured.\n\n"
        "nodo does not build services locally anymore — packing is done by a\n"
        "packer-service microVM (it runs Docker/buildx inside a sealed VM, so you\n"
        "never install Docker on this host).\n\n"
        "Point nodo at a running packer service, then re-run `nodo pack`:\n"
        "  • env var:  export PACKER_SERVICE_URL=http://<ip>:8080\n"
        "  • or config.yaml:\n"
        "        packer:\n"
        "          PACKER_SERVICE_URL: \"http://<ip>:8080\"\n\n"
        "The URL is the ip:port of a packer-service instance (API on :8080). Run\n"
        "your own (celaut-packer-service) and `nodo execute` it to get its IP, or\n"
        "use a shared one.\n"
    )


def __remove_path(path):
    import shutil
    if os.path.exists(path):
        (os.remove if os.path.isfile(path) else shutil.rmtree)(path)
        print(f"Removed: '{path}'")


def pack(directory: str) -> Optional[str]:
    packer_url = _resolve_packer_url()
    if not packer_url:
        print(_no_packer_configured_message())
        return None

    _id: Optional[str] = None
    is_remote, directory = prepare_directory(directory)

    service_zip_dir: str = generate_service_zip(project_directory=directory)

    bee_path: Optional[str] = None
    try:
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

        _id = import_bee(path=bee_path)

        if not _id:
            _msg = f"Failed to import the packed service for {directory}."
            print(_msg)
            raise Exception(_msg)

        if service_id_header and _id != service_id_header:
            print(
                "WARNING: imported service id does not match the packer's "
                f"X-Service-Id header (imported {_id}, header {service_id_header})."
            )

    except requests.exceptions.ConnectionError as e:
        print(
            f"\nCould not reach the packer service at {packer_url}: {e}\n"
            "Check PACKER_SERVICE_URL / packer.PACKER_SERVICE_URL and that the "
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

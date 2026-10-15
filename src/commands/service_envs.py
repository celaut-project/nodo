"""``nodo service_envs <service> [--json]``: the env vars a service asks for.

What ``nodo execute`` compares ``-e`` against (``src.utils.service_envs``), as a
command of its own so ``nodo tui`` can ask for the missing ones in a form before it
runs ``nodo execute``. A service not held locally is acquired first, the way
``execute`` would.
"""
import contextlib
import sys

from src.commands._catalogue import emit_error, emit_json
from src.commands.execute import acquire_service, resolve_service_hash
from src.utils import service_envs
from src.utils.registry_errors import ServiceRegistryError
from src.utils.utils import load_service_from_disk


def service_envs_command(service: str, as_json: bool = False) -> bool:
    resolved = resolve_service_hash(service)
    if not resolved:
        # Acquiring prints its progress; with --json stdout carries one object only.
        with contextlib.redirect_stdout(sys.stderr if as_json else sys.stdout):
            if acquire_service(service):
                resolved = resolve_service_hash(service)
    if not resolved:
        return emit_error(as_json, f"❌ Service {service} is not on this node.")

    try:
        specs = service_envs.env_specs(load_service_from_disk(service_hash=resolved))
    except ServiceRegistryError as e:
        return emit_error(as_json, f"❌ Could not read the service: {e}")

    if as_json:
        emit_json({"service": resolved, "envs": [spec.to_json() for spec in specs]})
        return True

    if not specs:
        print("The service declares no env vars.", flush=True)
        return True
    for spec in specs:
        need = f"required by network {'; '.join(spec.networks)}" if spec.required else "optional"
        print(f"{spec.name} ({need})", flush=True)
        if spec.tags:
            print(f"  format: {', '.join(spec.tags)}", flush=True)
        if spec.prose:
            print(f"  {spec.prose}", flush=True)
    return True

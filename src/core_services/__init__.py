"""Core services: Celaut services the node treats as part of its own workflow.

Core services are referenced by service id (content hash), configured under the
top-level ``core_services`` list in ``config.yaml`` as ``{name, id}`` entries.
They let the node bootstrap capabilities it does not ship with — for example,
resolving and auto-downloading a service that ``nodo execute`` is asked to run
but does not have locally (see :mod:`src.core_services.source_application`).
"""

from typing import Optional

from src.utils.config import ConfigManager

# Placeholder kept in ``config.example.yaml`` until a core service id is published.
# A core service whose id is unset or still set to this value is treated as "not
# configured" so the node fails closed instead of inventing/trusting an id.
UNSET_PLACEHOLDER = "<SET_ME>"

# Well-known core service roles (the ``name`` field of each entry).
SOURCE_APPLICATION = "source-application"
# The packer service (``nodo pack`` delegates the Docker build to it). Configured
# by id like any other core service so the node can download+launch it on demand
# via :func:`src.core_services.runtime.ensure_core_service_running`.
PACKER = "packer"
# Opportunistic best-effort tenant the node runs only when it has spare capacity
# and no real workloads; always preempted by real/paid execute requests. See
# :mod:`src.core_services.low_demand` and ``docs/design/low-demand-fallback.md``.
LOW_DEMAND_FALLBACK = "low-demand-fallback"

_env_manager = ConfigManager()


def get_core_service_id(name: str) -> Optional[str]:
    """Return the configured service id for the core service role ``name``.

    Returns ``None`` when ``core_services`` is missing/empty, has no entry for
    ``name``, or the entry's id is unset or still the ``"<SET_ME>"`` placeholder.
    Callers must treat ``None`` as "this capability is not configured" and degrade
    gracefully rather than fabricate an id.
    """
    entries = _env_manager.get("core_services", []) or []
    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") != name:
            continue
        service_id = str(entry.get("id", "") or "").strip()
        if not service_id or service_id == UNSET_PLACEHOLDER:
            return None
        return service_id

    return None

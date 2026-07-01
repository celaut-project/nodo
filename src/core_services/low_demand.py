"""Opportunistic **low-demand fallback** scheduler (WIP scaffold — DRAFT).

Concept
-------
A node should not sit idle. When it has spare resources (CPU, RAM, …) and is not
serving any real/paid workloads, it can *opportunistically* run a designated
``low-demand-fallback`` core service — a best-effort background tenant that soaks
up otherwise-wasted capacity. Two hard rules:

1. **Real workloads always preempt the fallback.** The instant a real ``execute``
   request arrives, or resources cross the configured thresholds, the fallback is
   stopped so the paid workload gets the capacity.
2. **Per-resource thresholds gate it.** Each tracked resource (CPU, RAM, and any
   future signal) has a config threshold under which the fallback may run. If any
   resource is above its threshold, the fallback does not start (and a running one
   is stopped).

Like the other core services, the fallback is referenced by service id in the
top-level ``core_services`` list (role name :data:`LOW_DEMAND_FALLBACK`) and
launched through :func:`src.core_services.runtime.ensure_core_service_running`,
reusing the exact framework from PR #120.

Status / contract
-----------------
* **This module is scaffold only and is NOT wired into the serve loop yet.** The
  exact scheduling/preemption mechanism needs sign-off. See the integration TODO
  in :func:`run_fallback_once` and ``docs/design/low-demand-fallback.md``.
* **Nothing here ever raises into the caller.** Every read (config, resource
  sampling) and every action (launch) is wrapped defensively and degrades to a
  safe default (``False`` / no-op). This matches the fail-closed tone of the rest
  of :mod:`src.core_services`.
* "Environment-variable threshold" from the spec maps to **config keys** under the
  ``low_demand:`` section of ``config.yaml`` (the node's env), consistent with
  every other tunable (e.g. ``timing.MANAGER_ITERATION_TIME``). There is no
  process-``os.environ`` override in :class:`ConfigManager` today; see open
  question 5 in the design doc.
"""

from typing import Optional

from src.core_services import LOW_DEMAND_FALLBACK, get_core_service_id
from src.utils.config import ConfigManager

_env_manager = ConfigManager()

# --- Config defaults (mirrored in config.example.yaml's ``low_demand:`` block) ---
_DEFAULT_ENABLED = False
_DEFAULT_POLL_INTERVAL = 30
_DEFAULT_CPU_MAX_PERCENT = 40.0
_DEFAULT_MEM_MAX_PERCENT = 60.0


def _get_float(key: str, default: float) -> float:
    """Read ``low_demand.<key>`` as a float, falling back to ``default``.

    Never raises: a missing key, wrong type, or unparseable value yields
    ``default`` so the scheduler always has usable thresholds.
    """
    try:
        value = _env_manager.get(f"low_demand.{key}", default)
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def is_enabled() -> bool:
    """Return whether the opportunistic fallback is switched on (``low_demand.ENABLED``).

    Defaults to ``False`` (opt-in) and never raises.
    """
    try:
        return bool(_env_manager.get("low_demand.ENABLED", _DEFAULT_ENABLED))
    except Exception:
        return _DEFAULT_ENABLED


def poll_interval_seconds() -> int:
    """Return the idle-check cadence in seconds (``low_demand.POLL_INTERVAL``).

    Used by the (future) serve-loop integration to decide how often to tick.
    Never raises; falls back to the default and clamps to a sane minimum of 1s.
    """
    try:
        return max(1, int(_get_float("POLL_INTERVAL", _DEFAULT_POLL_INTERVAL)))
    except Exception:
        return _DEFAULT_POLL_INTERVAL


def _current_cpu_percent() -> Optional[float]:
    """Best-effort current system CPU usage percent, or ``None`` if unavailable.

    Uses the non-blocking ``psutil.cpu_percent(interval=None)`` form (unlike
    ``power.py``'s blocking ``interval=1``) so a poll tick stays cheap; see open
    question 4 in the design doc about the warm-up-call caveat of the non-blocking
    form. Never raises.
    """
    try:
        import psutil

        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return None


def _current_mem_percent() -> Optional[float]:
    """Best-effort current system memory usage percent, or ``None`` if unavailable.

    Uses the simple system-wide ``psutil.virtual_memory().percent``. TODO (open
    question 1): optionally compare against the node's own RAM pool via
    ``src.manager.resources.IOBigData().get_ram_avaliable()`` /
    ``snapshot()['effective_available']``, which accounts for VM reservations.
    Never raises.
    """
    try:
        import psutil

        return float(psutil.virtual_memory().percent)
    except Exception:
        return None


def resources_below_threshold() -> bool:
    """Return ``True`` iff EVERY tracked resource is at/under its configured threshold.

    Reads the ``low_demand.CPU_MAX_PERCENT`` / ``low_demand.MEM_MAX_PERCENT``
    thresholds and compares them against the current system readings. The design is
    an **AND of per-resource checks**, so adding a new resource later (e.g. GPU) is
    a localized change: add its threshold + reading and ``and`` it in here.

    Fail-closed: if a resource reading is unavailable (``None``), that resource is
    treated as *not* satisfying its threshold, so the fallback is not started on
    incomplete information. Never raises.
    """
    cpu_max = _get_float("CPU_MAX_PERCENT", _DEFAULT_CPU_MAX_PERCENT)
    mem_max = _get_float("MEM_MAX_PERCENT", _DEFAULT_MEM_MAX_PERCENT)

    cpu = _current_cpu_percent()
    mem = _current_mem_percent()

    # Missing reading -> fail closed (do not run the fallback).
    if cpu is None or mem is None:
        return False

    return cpu <= cpu_max and mem <= mem_max


def _real_workload_present() -> bool:
    """Best-effort: is the node currently serving a real workload?

    Preemption trigger. First-cut (polled) implementation counts running internal
    instances via ``SQLConnection().get_all_internal_containers_ids()``
    (``src/database/sql_connection.py:479``). Any real workload means the fallback
    must not run.

    TODO (open question 3): (a) this counts the fallback's OWN instance too — tag
    the fallback instance (known ``instance_name``/father id) and exclude it here;
    (b) the lower-latency alternative is a reactive hook in
    ``Gateway.StartService`` (``src/gateway/gateway.py:28``) that stops the
    fallback immediately when a real request arrives, rather than waiting for the
    next poll tick. Never raises; on error assume "busy" (fail closed).
    """
    try:
        from src.database.sql_connection import SQLConnection

        ids = SQLConnection().get_all_internal_containers_ids() or []
        # TODO: exclude the fallback's own instance id/token once it is tagged.
        return len(ids) > 0
    except Exception:
        # Unknown -> assume busy so we never contend with a possible real workload.
        return True


def should_run_fallback() -> bool:
    """Decide whether the fallback SHOULD be running right now.

    Returns ``True`` only when ALL of:
        * ``low_demand.ENABLED`` is true, and
        * a non-placeholder ``low-demand-fallback`` id is configured in
          ``core_services`` (else fail closed — never invent an id), and
        * :func:`resources_below_threshold` is true, and
        * no real workload is present (:func:`_real_workload_present` is false).

    Never raises.
    """
    try:
        if not is_enabled():
            return False
        if get_core_service_id(LOW_DEMAND_FALLBACK) is None:
            return False
        if not resources_below_threshold():
            return False
        if _real_workload_present():
            return False
        return True
    except Exception:
        return False


def run_fallback_once() -> Optional[str]:
    """One scheduler tick: start the fallback if it should run; report its endpoint.

    Behaviour:
        * If :func:`should_run_fallback` is ``True``, best-effort launch/resume the
          fallback via
          :func:`src.core_services.runtime.ensure_core_service_running`
          (idempotent — it returns an already-running instance's endpoint without
          relaunching), and return that endpoint (or ``None`` if it couldn't be
          brought up).
        * Otherwise return ``None`` and do nothing here.

    **Preemption is NOT yet implemented in this scaffold.** When
    :func:`should_run_fallback` is false and a fallback instance is running, a real
    implementation must STOP it via ``stop_instance(token=...)``
    (``src/manager/manager.py:531``). That needs the fallback's instance token,
    which ``ensure_core_service_running``/``find_running_endpoint`` do not return
    today — see open question 2 (propose a ``find_running_instance(service_id)``
    helper in ``runtime.py``, or have the scheduler remember the token it launched).

    INTEGRATION TODO (not wired yet — needs Josemi's sign-off):
        Drive this from the manager loop in
        ``src/manager/maintain.py`` — inside ``manager_thread``'s ``while True:``
        at ``src/manager/maintain.py:269`` (which already sleeps
        ``MANAGER_ITERATION_TIME`` at line 288). Add a guarded call roughly like::

            from src.core_services.low_demand import run_fallback_once
            try:
                run_fallback_once()
            except Exception:
                pass

        (respecting ``poll_interval_seconds()`` cadence), plus the preemption
        branch once the stop path (open question 2) is decided. Reusing the
        existing manager thread avoids spawning another thread; alternatively a
        dedicated scheduler thread could be started in ``src/serve.py:12``.

    Never raises.
    """
    try:
        if not should_run_fallback():
            return None

        service_id = get_core_service_id(LOW_DEMAND_FALLBACK)
        if not service_id:
            return None

        from src.core_services.runtime import ensure_core_service_running

        return ensure_core_service_running(service_id, launch=True)
    except Exception:
        return None

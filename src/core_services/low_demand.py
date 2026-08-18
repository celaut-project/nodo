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
top-level ``core_services`` mapping (role name :data:`LOW_DEMAND_FALLBACK`) and
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

import time
from typing import Optional

from src.core_services import LOW_DEMAND_FALLBACK, get_core_service_id
from src.utils.config import ConfigManager

_env_manager = ConfigManager()

# --- Config defaults (mirrored in config.example.yaml's ``low_demand:`` block) ---
_DEFAULT_ENABLED = False
_DEFAULT_POLL_INTERVAL = 30
_DEFAULT_CPU_MAX_PERCENT = 40.0
_DEFAULT_MEM_MAX_PERCENT = 60.0
# Hysteresis: how many consecutive below-threshold, idle polls are required before the
# fallback is actually started, to avoid flapping on brief resource dips.
_DEFAULT_CONSECUTIVE_POLLS = 3


class _SchedulerState:
    """In-memory scheduler state for the manager-loop-driven fallback tick.

    Kept as a module singleton (mirrors ``ConfigManager()`` / ``IOBigData()`` usage
    elsewhere) so the tick can carry hysteresis + the launched instance's stop token
    across manager iterations without a new thread or persistent storage.
    """

    def __init__(self) -> None:
        # Consecutive below-threshold, idle polls observed so far (hysteresis counter).
        self.consecutive_below = 0
        # Monotonic timestamp of the last tick that actually ran, or ``None`` if never.
        self.last_tick_monotonic: Optional[float] = None
        # Stop token of the fallback instance we launched, so we can (a) exclude it when
        # counting "real" workloads and (b) STOP it on preemption. ``None`` if not running.
        self.running_token: Optional[str] = None


_state = _SchedulerState()


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


def consecutive_polls() -> int:
    """Return the hysteresis depth (``low_demand.LOW_DEMAND_CONSECUTIVE_POLLS``).

    The number of consecutive below-threshold, idle polls required before the fallback
    is started. Never raises; falls back to the default and clamps to a minimum of 1
    (1 == no hysteresis, start on the first clean poll).
    """
    try:
        return max(1, int(_get_float("LOW_DEMAND_CONSECUTIVE_POLLS", _DEFAULT_CONSECUTIVE_POLLS)))
    except Exception:
        return _DEFAULT_CONSECUTIVE_POLLS


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

    Uses the simple system-wide ``psutil.virtual_memory().percent``. This is the
    *system-wide* half of the RAM gate; it is cross-checked against the node's own
    reservation accounting in :func:`_iobigdata_has_headroom` (Josemi decision #1).
    Never raises.
    """
    try:
        import psutil

        return float(psutil.virtual_memory().percent)
    except Exception:
        return None


def _iobigdata_has_headroom() -> bool:
    """Return ``True`` iff the node's own RAM pool still has positive availability.

    RAM gate, part 2 (Josemi decision #1): cross-check the system-wide
    ``psutil.virtual_memory().percent`` against the node's ``IOBigData`` reservation
    accounting. Even when the system-wide percent looks fine, the node's pool may
    already be fully reserved by in-flight VM locks; ``snapshot()['effective_available']``
    (pool minus locked) captures that. We require it to be strictly positive.

    Fail-closed: any error (import failure, singleton/snapshot error, missing key)
    yields ``False`` so the fallback is not started on incomplete information. Never
    raises.
    """
    try:
        from src.manager.resources import IOBigData

        snapshot = IOBigData().snapshot()
        return int(snapshot.get("effective_available", 0)) > 0
    except Exception:
        return False


def resources_below_threshold() -> bool:
    """Return ``True`` iff EVERY tracked resource is at/under its configured threshold.

    Reads the ``low_demand.CPU_MAX_PERCENT`` / ``low_demand.MEM_MAX_PERCENT``
    thresholds and compares them against the current system readings. The design is
    an **AND of per-resource checks**, so adding a new resource later (e.g. GPU) is
    a localized change: add its threshold + reading and ``and`` it in here.

    Gates (Josemi decision #1 — FINAL):
        * **CPU:** ``psutil.cpu_percent`` <= ``CPU_MAX_PERCENT``.
        * **RAM:** BOTH ``psutil.virtual_memory().percent`` <= ``MEM_MAX_PERCENT``
          AND the node's own ``IOBigData`` pool has positive ``effective_available``
          (:func:`_iobigdata_has_headroom`) — the system-wide percent is cross-checked
          against the node's reservation accounting so a fully-reserved pool blocks the
          start even when system RAM looks free.

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

    if cpu > cpu_max or mem > mem_max:
        return False

    # RAM cross-check against the node's own reservation accounting.
    if not _iobigdata_has_headroom():
        return False

    return True


def _real_workload_present() -> bool:
    """Best-effort: is the node currently serving a *real* workload?

    Preemption trigger, **polled** (Josemi decision #3 — FINAL): count running
    internal instances via ``SQLConnection().get_all_internal_containers_ids()``
    (``src/database/sql_connection.py:479``) on each tick. No gateway hooks/callbacks.
    Any real workload means the fallback must not run.

    The fallback's OWN instance is excluded: when we launch it we record its stop
    token in :data:`_state.running_token` (see :func:`_start_fallback`), and that
    token is filtered out here so the fallback never counts itself as a real
    workload (which would otherwise make it preempt itself and flap).

    Never raises; on error assume "busy" (fail closed) so we never contend with a
    possible real workload.
    """
    try:
        from src.database.sql_connection import SQLConnection

        ids = SQLConnection().get_all_internal_containers_ids() or []
        own = _state.running_token
        real = [i for i in ids if i != own]
        return len(real) > 0
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
    """Idempotently (re)launch the fallback if it should run; report its endpoint.

    Behaviour:
        * If :func:`should_run_fallback` is ``True``, best-effort launch/resume the
          fallback via
          :func:`src.core_services.runtime.ensure_core_service_running`
          (idempotent — it returns an already-running instance's endpoint without
          relaunching), record the launched instance's stop token for preemption,
          and return that endpoint (or ``None`` if it couldn't be brought up).
        * Otherwise return ``None`` and do nothing here.

    This is the *stateless launch primitive*. The hysteresis + preemption policy and
    the ``POLL_INTERVAL`` cadence live in :func:`scheduler_tick`, which is what the
    manager loop drives; ``run_fallback_once`` is the "make it running now" building
    block it and the tests use.

    Never raises.
    """
    try:
        if not should_run_fallback():
            return None

        service_id = get_core_service_id(LOW_DEMAND_FALLBACK)
        if not service_id:
            return None

        from src.core_services.runtime import ensure_core_service_running

        endpoint = ensure_core_service_running(service_id, launch=True)
        # Record the running instance's stop token so the tick can (a) exclude it from
        # "real workload" counting and (b) STOP it on preemption. Best-effort.
        _record_running_token(service_id)
        return endpoint
    except Exception:
        return None


# --- Scheduler tick (wired into src/manager/maintain.py:manager_thread) ------------


def _reset_hysteresis() -> None:
    """Reset the consecutive-below-threshold counter (call on any preemption/abort)."""
    _state.consecutive_below = 0


def _record_running_token(service_id: str) -> None:
    """Best-effort: remember the running fallback instance's stop token in ``_state``.

    Uses :func:`src.core_services.runtime.find_running_instance`. Never raises; on
    failure it simply leaves the previously known token in place.
    """
    try:
        from src.core_services.runtime import find_running_instance

        info = find_running_instance(service_id)
        if info:
            _state.running_token = info
    except Exception:
        pass


def _stop_running_fallback() -> None:
    """STOP (never pause) the running fallback instance, if any (Josemi decision #2).

    There is no suspend/pause primitive in Celaut, so preemption is always a full
    ``stop_instance(token=...)`` (``src/manager/manager.py:531``). Uses the token we
    recorded at launch; if we don't have one, best-effort discovers a running fallback
    instance via :func:`src.core_services.runtime.find_running_instance`. Never raises.
    """
    token = _state.running_token
    if not token:
        try:
            service_id = get_core_service_id(LOW_DEMAND_FALLBACK)
            if service_id:
                from src.core_services.runtime import find_running_instance

                info = find_running_instance(service_id)
                if info and info[0]:
                    token = info[0]
        except Exception:
            token = None

    if not token:
        _state.running_token = None
        return

    try:
        from src.manager.manager import stop_instance

        stop_instance(token=token)
    except Exception:
        # Stop failed (no daemon, already gone, …) — nothing else we can safely do.
        pass
    finally:
        _state.running_token = None


def scheduler_tick() -> None:
    """One manager-loop iteration of the opportunistic fallback scheduler.

    Called every ``manager_thread`` iteration (``src/manager/maintain.py``); it
    self-gates to the configured ``POLL_INTERVAL`` cadence so calling it more often
    than that is a cheap no-op. Implements the three FINAL decisions:

    #1 (resource signals): start only after
       :data:`consecutive_polls` consecutive polls with
       :func:`resources_below_threshold` true (CPU% + system RAM% + IOBigData pool
       headroom) AND no real workload — hysteresis to avoid flapping.
    #2 (stop vs pause): preemption ALWAYS STOPS via :func:`_stop_running_fallback`
       (there is no pause primitive).
    #3 (request detection): POLLED — :func:`_real_workload_present` reads node state
       each tick; no gateway hooks.

    Fully defensive: any failure is swallowed so the manager loop is never disturbed.
    Never raises.
    """
    try:
        if not is_enabled():
            return

        # Cadence gate: only do real work every POLL_INTERVAL seconds, independent of
        # the (faster) MANAGER_ITERATION_TIME the manager loop ticks at.
        now = time.monotonic()
        last = _state.last_tick_monotonic
        if last is not None and (now - last) < poll_interval_seconds():
            return
        _state.last_tick_monotonic = now

        # No configured id -> nothing to run; make sure nothing lingers and reset.
        if get_core_service_id(LOW_DEMAND_FALLBACK) is None:
            _reset_hysteresis()
            _stop_running_fallback()
            return

        busy = _real_workload_present()
        under_threshold = resources_below_threshold()

        # Preemption: a real workload OR resources over threshold => STOP immediately
        # and reset the hysteresis counter (decisions #2 + #3).
        if busy or not under_threshold:
            _reset_hysteresis()
            _stop_running_fallback()
            return

        # Idle + under threshold: require N consecutive clean polls before starting.
        _state.consecutive_below += 1
        if _state.consecutive_below < consecutive_polls():
            return

        # Hysteresis satisfied — (idempotently) ensure the fallback is running.
        run_fallback_once()
    except Exception:
        return

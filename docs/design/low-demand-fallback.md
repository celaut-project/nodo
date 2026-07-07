# Low-Demand Fallback Core Service — Design

Status: **decisions FINAL (Josemi signed off); scheduler wired.** This document
specifies a new core service and its node-side scheduler. The three previously-open
design questions (resource signals, stop-vs-pause, request detection) are now
resolved — see §5. The scheduler is **wired into the manager loop**
(`src/manager/maintain.py:manager_thread`) and is a no-op unless
`low_demand.ENABLED` is set, so existing nodes are unaffected until an operator opts
in.

This design builds on the `core_services` framework introduced in
**PR #120** (branch `feat/core-services-execute-source-application`).

---

## 1. Concept

A node should not sit idle. When it has spare resources (CPU, RAM, …) and is not
serving any real/paid workloads, it can *opportunistically* run a designated
**low-demand fallback** core service — think of it as a background/best-effort
tenant that soaks up otherwise-wasted capacity (e.g. a batch, mining, indexing,
or reputation-crunching service).

Two hard rules:

1. **Real workloads always preempt the fallback.** The instant a real `execute`
   request arrives (a `StartService` on the gateway), or resources cross the
   configured thresholds, the fallback must be stopped/paused so the paid
   workload gets the capacity.
2. **Per-resource thresholds gate it.** For each resource the node tracks (CPU,
   RAM, and any future signal), an env-var/config threshold defines the level
   under which the fallback is allowed to run. If *any* resource is above its
   threshold, the fallback does not start (and a running one is stopped).

Like the other core services, it is referenced by service id (content hash) in
the top-level `core_services:` list — never by URL — so trust and resolution
reuse the exact framework from PR #120.

### Proposed role name

```
low-demand-fallback
```

Added as `LOW_DEMAND_FALLBACK = "low-demand-fallback"` alongside
`SOURCE_APPLICATION` in `src/core_services/__init__.py`.

---

## 2. Where things live in the codebase (grounding)

Everything below is a real file:line found while researching, so the integration
is concrete rather than hand-wavy.

### 2.1 The serve loop / daemon entrypoint (where the scheduler goes)

- `src/serve.py:12` — `serve()` starts the manager on a daemon thread
  (`threading.Thread(target=manager_thread, daemon=True).start()`) and then runs
  the gRPC gateway server.
- `src/manager/maintain.py:250` — `manager_thread()` is the node's background
  loop. Its `while True:` at `src/manager/maintain.py:269` already runs periodic
  maintenance (`maintain_vmachines`, `maintain_clients`, `peer_deposits`, …) and
  sleeps `MANAGER_ITERATION_TIME` seconds (`src/manager/maintain.py:288`).

  **This is the home for the fallback scheduler tick (now wired).** A single
  guarded `scheduler_tick()` call is added inside that loop; it self-gates to
  `low_demand.POLL_INTERVAL` and gives a periodic idle-check without spawning
  another thread.

### 2.2 Resource signals the node already measures

- **RAM:** `src/manager/resources.py` — `IOBigData` (singleton) tracks the RAM
  pool. `IOBigData().snapshot()` (`src/manager/resources.py:99`) returns
  `system_available`, `pool_available`, `ram_locked`, `effective_available`.
  `psutil.virtual_memory()` is used directly there too. `get_ram_avaliable()`
  (`resources.py:76`) = pool minus locked. This is the RAM signal to compare
  against the RAM threshold.
- **CPU:** `psutil.cpu_percent(...)` is already used in several places:
  `src/manager/power.py:48` (`get_system_metrics` returns `cpu_percent` and
  `memory_usage` = `memory.percent`), and in the cost functions
  (`src/utils/cost_functions/generate_estimated_cost.py:32`,
  `src/utils/cost_functions/execution_cost.py:145`). CPU availability is
  computed as `100 - psutil.cpu_percent(...)`.
- **"How busy is the node" (running workloads):**
  `SQLConnection().get_all_internal_containers_ids()`
  (`src/database/sql_connection.py:479`) lists running internal instances;
  `internal_instance_exists(id)` (`sql_connection.py:513`) checks one.

  Note `power.py`'s `get_system_metrics()` is a ready-made single call that
  returns both `cpu_percent` and `memory_usage` (percent). We could reuse it, but
  it does a blocking `psutil.cpu_percent(interval=1)`; for a poll loop we may
  prefer the non-blocking form. **(open question 4)**

### 2.3 Starting the fallback (launch path)

- `src/core_services/runtime.py` —
  `ensure_core_service_running(service_id, *, launch=True)`
  resolves an already-running instance (`find_running_endpoint`), else
  best-effort downloads via the source-application, else launches via the
  canonical `nodo execute` path (`src/commands/execute.py:116`). Fully
  defensive, never raises. **The fallback launches through exactly this.**

### 2.4 Stopping the fallback (preemption path)

- `src/manager/manager.py:531` — `stop_instance(token: str)` is the canonical
  stop. It handles internal and external instances, refunds gas, purges DB, and
  calls `kill(vmachine_id=...)`.
- Gateway exposes it: `Gateway.StopService` at `src/gateway/gateway.py:31` calls
  `stop_instance(token=token)`.

  To stop the fallback we need its **token/instance id**. `find_running_endpoint`
  (runtime.py) currently returns only the endpoint; to preempt we also need the
  instance token so we can call `stop_instance`. **(open question 2 — we propose
  adding a small `find_running_instance(service_id) -> (token, endpoint)` helper,
  or having the scheduler track the token it launched.)**

### 2.5 The preemption trigger (a real request arriving)

- `src/gateway/gateway.py:28` — `Gateway.StartService` is *the* entrypoint for
  an incoming (real) execute request; it delegates to `StartServiceIterable`.

  This is the signal that must preempt the fallback. Two options were considered:
  - **(a) Reactive:** hook `StartService` so that, before/at the start of a real
    launch, it stops any running fallback instance. Lowest-latency preemption.
  - **(b) Polled:** the scheduler in `manager_thread` notices a new real
    internal instance (via `get_all_internal_containers_ids()` growing) and stops
    the fallback on the next poll (≤ `POLL_INTERVAL`). Simpler, purely additive.

  **Decision (§5.3, FINAL): (b) polled.** No gateway edits; the fallback's own
  instance is excluded from the "real workload" count via its recorded launch
  token. (a) remains a possible low-latency upgrade later.

### 2.6 Config / env-var convention

- `src/utils/config.py:156` — `ConfigManager.get("a.b.C")` reads nested YAML via
  dot notation, and also resolves a bare top-level key by scanning sections.
  Values come from `config.yaml` (loaded from `config.example.yaml`).
  **Note:** there is *no* `os.environ` override inside `get()` today — "env var"
  in the spec maps to **config keys** in `config.yaml` (the node's env), which is
  how every other tunable works (e.g. `packer.PACKER_MEMORY_SIZE_FACTOR`,
  `timing.MANAGER_ITERATION_TIME`). We follow that convention: a `low_demand:`
  section with per-resource threshold keys. **(open question 5: if literal
  process env-var overrides are wanted, that's a separate ConfigManager change.)**

---

## 3. Proposed config block

Added to `config.example.yaml` next to the other tunables, plus the
`core_services` entry:

```yaml
core_services:
  - name: "source-application"
    id: "<SET_ME>"
  - name: "packer"
    id: "<SET_ME>"
  - name: "low-demand-fallback"      # NEW
    id: "<SET_ME>"

low_demand:
  ENABLED: false            # master switch; opportunistic fallback is OFF by default
  POLL_INTERVAL: 30         # seconds between idle checks (independent of MANAGER_ITERATION_TIME)
  CPU_MAX_PERCENT: 40       # run only when system CPU usage is <= this
  MEM_MAX_PERCENT: 60       # run only when system memory usage is <= this
  # Future per-resource thresholds go here (e.g. GPU_MAX_PERCENT, DISK_MAX_PERCENT).
```

Rationale for defaults: `ENABLED: false` so nothing changes for existing nodes
until an operator opts in; conservative CPU/MEM ceilings so a real workload has
clear headroom.

---

## 4. Scheduler lifecycle (proposed)

Per tick (every `low_demand.POLL_INTERVAL`, driven from `manager_thread`):

```
if not low_demand.ENABLED:            -> do nothing
if get_core_service_id("low-demand-fallback") is None:  -> do nothing (fail closed)

busy = a real workload is present  (open question 3: reactive hook vs. polled)
under_threshold = resources_below_threshold()   # CPU% <= CPU_MAX and MEM% <= MEM_MAX and ...

if under_threshold and not busy:
    ensure_core_service_running(id)   # launch/resume the fallback (idempotent)
else:
    stop the running fallback instance (preempt), if any
```

Key properties:

- **Idempotent start:** `ensure_core_service_running` returns the endpoint of an
  already-running instance without relaunching, so calling it every tick is safe.
- **Preemption:** any of {resources over threshold, a real request present}
  triggers stopping the fallback. With the reactive option (2.5a) a real
  `StartService` also stops it immediately, not just on the next tick.
- **Defensive:** the whole tick is wrapped so a failure never disturbs the
  manager loop (matches the tone of the rest of `core_services`).

---

## 5. Resolved decisions (Josemi — FINAL)

The three design questions below were put to maintainer Josemi and are now
**decided and implemented**. Items 4–7 are recorded for completeness.

1. **Resource signals & thresholds — FINAL: CPU% + RAM% + hysteresis, delegated
   to us.** Poll every `POLL_INTERVAL` (default 30s) *inside the existing
   `manager_thread` loop* (no new thread). Gate the low-demand start on BOTH:
   - **CPU:** `psutil.cpu_percent` `<=` `CPU_MAX_PERCENT` (default 40), and
   - **RAM:** the node's `IOBigData` reservation accounting
     (`snapshot()['effective_available'] > 0`, pool minus VM locks) **cross-checked
     with** `psutil.virtual_memory().percent <= MEM_MAX_PERCENT` (default 60).

   Plus **hysteresis**: require `LOW_DEMAND_CONSECUTIVE_POLLS` (default 3)
   consecutive below-threshold, idle polls before starting, to avoid flapping.
   Implemented in `resources_below_threshold()` / `_iobigdata_has_headroom()` and
   the hysteresis counter in `scheduler_tick()`.

2. **Stop vs. pause — FINAL: ALWAYS STOP.** There is no suspend/pause primitive in
   Celaut today. When a real `execute` request arrives, or resources exceed a
   threshold, the low-demand instance is **stopped immediately** via
   `stop_instance()` (`manager.py:531`) — never paused/suspended. To get the
   instance's stop token we added `find_running_instance(service_id) -> (token,
   endpoint)` to `runtime.py`, and the scheduler also remembers the token it
   launched. Implemented in `_stop_running_fallback()`.

3. **Request detection — FINAL: POLLED, not reactive.** Pending/running real
   `execute` requests are detected by polling node state each tick
   (`SQLConnection().get_all_internal_containers_ids()`); **no** gateway
   event hooks/callbacks are added. The fallback's own instance is excluded from
   the count via the recorded launch token so it never preempts itself.
   Implemented in `_real_workload_present()`.

### Recorded for completeness (not blocking)

4. **CPU sampling cost.** The poll uses the non-blocking
   `psutil.cpu_percent(interval=None)` form (cheap per tick; the blocking
   `interval=1` form in `power.py:48` is avoided).
5. **Config vs. literal env vars.** Thresholds live as `config.yaml` keys under
   `low_demand:` (consistent with every other tunable). Literal OS-env overrides
   would be a separate `ConfigManager` change and are out of scope.
6. **GPU / other resources.** Out of scope for the first cut; the
   `resources_below_threshold()` design is a per-resource AND-of-checks so adding
   `GPU_MAX_PERCENT` later is a localized change.
7. **Where to drive the tick — FINAL: reuse `manager_thread`.** The tick is a
   single guarded `scheduler_tick()` call in the existing loop
   (`maintain.py`), which self-gates to `POLL_INTERVAL`. No dedicated thread.

---

## 6. What ships in this PR

- `LOW_DEMAND_FALLBACK` role constant in `src/core_services/__init__.py`.
- `low-demand-fallback` entry (`<SET_ME>`) + a documented `low_demand:` block in
  `config.example.yaml`, including `LOW_DEMAND_CONSECUTIVE_POLLS`.
- `src/core_services/low_demand.py` — `resources_below_threshold()` (CPU + RAM +
  IOBigData cross-check), `_real_workload_present()` (polled, excludes the
  fallback's own instance), `should_run_fallback()`, `run_fallback_once()`, and the
  stateful `scheduler_tick()` (hysteresis + always-stop preemption + `POLL_INTERVAL`
  cadence). Heavily documented, never raises.
- `src/core_services/runtime.py` — `find_running_instance(service_id) -> (token,
  endpoint)` so preemption can `stop_instance()` the fallback.
- `src/manager/maintain.py` — a single guarded `scheduler_tick()` call wired into
  the existing `manager_thread` loop (no new thread; no-op unless `ENABLED`).
- `tests/test_low_demand.py` — unit tests with mocked resource readings, a mocked
  `ensure_core_service_running`, and coverage of hysteresis start, over-threshold
  no-start, real-request-preempts-STOP, and polled detection (no real launch/network).

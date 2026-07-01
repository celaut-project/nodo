# Low-Demand Fallback Core Service — Design (WIP / DRAFT)

Status: **proposal for Josemi's sign-off.** This document specifies a new core
service and its node-side scheduler. The scaffold that accompanies it
(`src/core_services/low_demand.py`, config entries, tests) is intentionally
**not wired into the serve loop** — the exact scheduling/preemption mechanism is
the thing we want to agree on before implementing it for real.

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

  **This is the natural home for the fallback scheduler tick.** A single call
  such as `run_fallback_once()` added inside that loop (guarded, best-effort)
  gives us a periodic idle-check without spawning another thread. See the
  integration TODO in `src/core_services/low_demand.py`.

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

  This is the signal that must preempt the fallback. Options (open question 3):
  - **(a) Reactive:** hook `StartService` so that, before/at the start of a real
    launch, it stops any running fallback instance. Lowest-latency preemption.
  - **(b) Polled:** the scheduler in `manager_thread` notices a new real
    internal instance (via `get_all_internal_containers_ids()` growing, or a
    dedicated "real workload present" flag) and stops the fallback on the next
    tick (≤ `MANAGER_ITERATION_TIME`, default 10s). Simpler, but up to one tick
    of contention.

  The scaffold assumes (b) for the first cut (no gateway edits, purely additive)
  and leaves (a) as the low-latency upgrade for Josemi to choose.

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

## 5. Open questions for Josemi (decisions needed)

1. **Exact resource signals & thresholds.** Confirm CPU% + MEM% are the right
   first cut, and whether the RAM threshold should compare against
   `psutil.virtual_memory().percent` (simple, system-wide) or the node's own
   `IOBigData().get_ram_avaliable()` pool (accounts for VM reservations). The
   scaffold uses the simple system-wide `psutil` percent and TODOs the pool
   variant.
2. **Stop vs. pause.** Should preemption fully `stop_instance()` the fallback
   (losing in-flight work, freeing all resources) or *pause/suspend* it (keep
   state, resume cheaply)? The node currently only has `stop_instance`
   (`manager.py:531`) + `kill`; there is no suspend primitive. If pause is
   required we need a new virtualizer capability. Also: to stop it we need the
   fallback's **instance token** — propose a `find_running_instance(service_id)`
   helper in `runtime.py` (open item 2.4).
3. **How to detect "a real request arrived."** Reactive hook into
   `Gateway.StartService` (`gateway.py:28`, lowest latency) vs. polled detection
   in `manager_thread` (simplest, ≤1 tick latency). Which do you want first?
   And how do we distinguish a *real* request from the fallback's own launch
   (which also goes through the execute path)? Proposal: tag the fallback
   instance (e.g. a known `instance_name`/father id) so the scheduler and the
   preemption check ignore it when counting "real" workloads.
4. **CPU sampling cost.** `psutil.cpu_percent(interval=1)` blocks 1s
   (as in `power.py:48`). For a poll loop we'd prefer the non-blocking
   `cpu_percent(interval=None)` (needs a warm-up call). Confirm acceptable.
5. **Config vs. literal env vars.** `ConfigManager.get()` has no process-env
   override today; the spec's "environment-variable threshold" is implemented as
   `config.yaml` keys under `low_demand:` (consistent with every other tunable).
   If you want real `LOW_DEMAND_CPU_MAX_PERCENT`-style OS env overrides, that's a
   separate ConfigManager enhancement — say the word.
6. **GPU / other resources.** Out of scope for the first cut; the
   `resources_below_threshold()` design is a per-resource AND-of-checks so adding
   `GPU_MAX_PERCENT` later is a localized change.
7. **Where to drive the tick.** Reuse `manager_thread` (`maintain.py:269`, no new
   thread — preferred) vs. a dedicated scheduler thread started in `serve.py:12`.
   Scaffold assumes the former.

---

## 6. What ships in this PR (scaffold only)

- `LOW_DEMAND_FALLBACK` role constant in `src/core_services/__init__.py`.
- `low-demand-fallback` entry (`<SET_ME>`) + a documented `low_demand:` block in
  `config.example.yaml`.
- `src/core_services/low_demand.py` — skeleton: `resources_below_threshold()`,
  `should_run_fallback()`, `run_fallback_once()`. Heavily documented, never
  raises, **not wired into the serve loop** (integration TODO points at
  `src/manager/maintain.py:269`).
- `tests/test_low_demand.py` — unit tests with mocked resource readings and a
  mocked `ensure_core_service_running` (no real launch/network).

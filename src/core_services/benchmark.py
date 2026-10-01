"""The optional ``benchmark`` core service: measure this node's per-core scores at startup.

A node's scores live in ``config.yaml`` under ``benchmark.BY_ARCH`` (see
:mod:`src.utils.benchmark`), with ``-1`` meaning "not measured". This module fills the
``-1``s and nothing else:

* **When.** Once, at startup, on a daemon thread started by ``serve()`` after the
  gateway is up (launching goes through it). Only while some architecture this node
  serves still has an unmeasured primitive, so a node whose scores are all written --
  measured earlier, or by hand -- boots nothing.
* **How.** The configured ``core_services.benchmark`` service
  (celaut-basics/demo-service, ``benchmark/``) is launched **on this node** -- the
  launch is pinned local through ``launch_service.FORCED_LOCAL``, since a peer that won
  the cost comparison would hand back its own numbers -- and asked
  ``GET /cgi-bin/benchmark?working_set_bytes=<n>``. It answers with the four scores
  and the architecture it ran under, and is stopped.
* **Per architecture.** A service is one architecture, so the entry is one id or a list
  of them, one per architecture packed. Each runs where the node runs that
  architecture -- Cloud Hypervisor for the host's, QEMU+TCG for a foreign one when
  emulation is ready -- which is exactly the difference the scores exist to show (#448).
  The answer is filed under the architecture the service *reports*, cross-checked
  against the one the service *declares* when that is known locally.
* **Never in the way.** Not configured -> nothing happens. Any failure (no gateway, the
  launch refused, the service never answering, a malformed answer) is logged and leaves
  the ``-1``s where they were, for the next start; nothing here raises into ``serve()``,
  and nothing touches billing. A value written by hand is never overwritten.

The admission side never comes here: it reads ``config.yaml`` through
:mod:`src.utils.benchmark` and nothing else, so it never boots anything.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.core_services import BENCHMARK, UNSET_PLACEHOLDER
from src.utils import benchmark, logger as log
from src.utils.arch_guard import arch_from_tags
from src.utils.config import ConfigManager

_env_manager = ConfigManager()

# The endpoint of the benchmark service (benchmark/www/cgi-bin/benchmark).
BENCHMARK_PATH = "/cgi-bin/benchmark"

# The node reports an instance ready once the guest's network answers, which under a
# microVM is seconds before the service inside binds its port; a connect is retried
# until then rather than read as a dead service.
READY_TIMEOUT_S = 180.0
READY_POLL_S = 1.0

# One run is ~10 s natively. Under QEMU+TCG every primitive is slower and the memory one
# can take minutes, so the read timeout is generous: a run that is still going is not a
# failure, and nothing waits on this thread.
REQUEST_TIMEOUT_S = 1800.0

# One measurement per node at a time. Two would each measure half a machine.
_RUN_LOCK = threading.Lock()


def configured_service_ids() -> List[str]:
    """The ids under ``core_services.benchmark``: one string, or a list of them.

    Empty when the entry is absent, empty, or still ``"<SET_ME>"`` -- the node then
    measures nothing, which is the feature being off rather than an error.
    """
    try:
        entries = _env_manager.get("core_services", {}) or {}
        raw = entries.get(BENCHMARK) if isinstance(entries, dict) else None
    except Exception:
        return []
    candidates = raw if isinstance(raw, (list, tuple)) else [raw]
    ids = []
    for candidate in candidates:
        service_id = str(candidate or "").strip()
        if service_id and service_id != UNSET_PLACEHOLDER and service_id not in ids:
            ids.append(service_id)
    return ids


def served_architectures() -> List[str]:
    """Canonical tags of what this node can boot right now (native + emulated)."""
    try:
        from src.utils.architectures import SUPPORTED_ARCHITECTURES
    except Exception:
        return []
    return [aliases[0] for aliases in SUPPORTED_ARCHITECTURES if aliases]


def pending_architectures(served: Sequence[str]) -> Dict[str, Tuple[str, ...]]:
    """Every served architecture with a primitive still at ``-1``, and which ones.

    The working-set key is not a primitive: it is filled alongside the bandwidth, and a
    bandwidth written by hand without one is left as written (and is then never
    accepted for enforcement -- see :func:`src.utils.benchmark.shortfalls`).
    An architecture the config lists but this node does not serve cannot be measured
    here; it is said once in the log and otherwise left alone.
    """
    pending: Dict[str, Tuple[str, ...]] = {}
    for arch in benchmark.configured_architectures():
        scores = benchmark.node_scores(arch)
        missing = tuple(k for k in benchmark.BENCHMARK_KEYS if scores[k] < 0)
        if not missing:
            continue
        if arch not in served:
            log.LOGGER(
                f"[BENCHMARK] {arch} has unmeasured scores ({', '.join(missing)}) but this "
                "node does not run that architecture, so they cannot be measured here."
            )
            continue
        pending[arch] = missing
    return pending


def requested_working_set(arch: Optional[str]) -> int:
    """The working set to ask the service to measure over for ``arch``.

    A size the operator wrote while the bandwidth is still ``-1`` is honoured: that is
    how a node chooses to be scored over a larger set. Otherwise the pinned default.
    """
    if arch:
        scores = benchmark.node_scores(arch)
        if scores[benchmark.MEM_BANDWIDTH_KEY] < 0 and scores[benchmark.MEM_WORKING_SET_KEY] > 0:
            return scores[benchmark.MEM_WORKING_SET_KEY]
    return benchmark.DEFAULT_MEM_BANDWIDTH_WORKING_SET_BYTES


def parse_answer(body: bytes) -> Tuple[Optional[str], Dict[str, int]]:
    """The service's JSON answer as ``(architecture, scores)``.

    Only :data:`benchmark.SCORE_KEYS` holding a non-negative integer are kept; anything
    else in the answer (``skipped``, a key from a newer service) is ignored. The
    architecture is canonicalised, and None when the answer does not name one this node
    knows -- in which case nothing it says can be filed anywhere. Raises ``ValueError``
    on a body that is not a JSON object.
    """
    answer = json.loads(body.decode("utf-8"))
    if not isinstance(answer, dict):
        raise ValueError("the benchmark service did not answer with a JSON object")
    arch = arch_from_tags([answer.get("architecture", "")])
    scores = {
        key: value for key, value in answer.items()
        if key in benchmark.SCORE_KEYS
        and isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }
    return arch, scores


def merge_scores(current: Mapping[str, int], measured: Mapping[str, int]) -> Tuple[Dict[str, int], List[str]]:
    """``current`` with its ``-1``s filled from ``measured``, and the keys filled.

    Never touches a value that is not ``-1``. The bandwidth and its working set go in
    together or not at all: a bandwidth without the set it was measured over is a score
    nothing may be compared against.
    """
    merged = {key: current.get(key, benchmark.UNMEASURED) for key in benchmark.SCORE_KEYS}
    filled: List[str] = []
    for key in benchmark.BENCHMARK_KEYS:
        if merged[key] >= 0 or key not in measured:
            continue
        if key == benchmark.MEM_BANDWIDTH_KEY:
            if benchmark.MEM_WORKING_SET_KEY not in measured:
                continue
            merged[benchmark.MEM_WORKING_SET_KEY] = measured[benchmark.MEM_WORKING_SET_KEY]
            filled.append(benchmark.MEM_WORKING_SET_KEY)
        merged[key] = measured[key]
        filled.append(key)
    return merged, filled


def write_scores(arch: str, measured: Mapping[str, int]) -> List[str]:
    """Fill ``arch``'s ``-1``s in config.yaml from ``measured``; returns the keys written.

    Read back from the config right before writing, and written as one block, so a
    single save (and a single timestamped backup) covers the architecture.
    """
    merged, filled = merge_scores(benchmark.node_scores(arch), measured)
    if filled:
        _env_manager.set(f"{benchmark.CONFIG_KEY}.{arch}", merged)
    return filled


def declared_architecture(service_id: str) -> Optional[str]:
    """The architecture a service declares, when it is in the local registry already."""
    try:
        from src.utils.utils import read_service_from_disk

        service = read_service_from_disk(service_id)
    except Exception:
        return None
    if service is None:
        return None
    return arch_from_tags(service.container.architecture.tags)


# --- The three side effects, each replaceable in a test ------------------------------


def launch_local(service_id: str):
    """Launch ``service_id`` on this node, never on a peer; the ``ServiceInstance``.

    Goes through this node's own gateway like ``nodo execute`` (a dev client pays,
    with no real money), carrying a one-time forced-peer hint that pins it local. The
    service is downloaded through the source-application first if it is not in the
    registry. Raises on any failure; the caller logs it.
    """
    from protos import celaut_pb2
    from src.commands.execute import DEV_CLIENT_FUNDING_MU, resolve_service_hash
    from src.core_services.source_application import acquire_service
    from src.database.sql_connection import SQLConnection
    from src.gateway.launcher.launch_service import FORCED_LOCAL
    from src.identity.grpc_transport import local_channel
    from src.manager.manager import get_execute_client
    from src.utils.bee_client import BeeClient
    from src.utils.hashing import get_configured_hash_id

    resolved = resolve_service_hash(service_id)
    if not resolved and acquire_service(service_id):
        resolved = resolve_service_hash(service_id)
    if not resolved:
        raise RuntimeError(
            f"service {service_id} is not in the registry and could not be acquired"
        )

    def _request(token: str):
        yield celaut_pb2.Client(client_id=get_execute_client(amount_mu=DEV_CLIENT_FUNDING_MU))
        yield celaut_pb2.RecursionGuard(token=token)
        yield celaut_pb2.Configuration()
        yield celaut_pb2.Metadata.HashTag.Hash(
            type=get_configured_hash_id(_env_manager), value=bytes.fromhex(resolved)
        )

    sc = SQLConnection()
    token = uuid.uuid4().hex
    sc.set_forced_execution_peer(token=token, peer_id=FORCED_LOCAL)
    channel = local_channel()
    try:
        return BeeClient.start_service(channel, _request(token))
    finally:
        # launch_service consumes the hint; this only deletes it when the call never
        # got that far.
        sc.pop_forced_execution_peer(token)
        channel.close()


def instance_endpoint(service_instance) -> Optional[Tuple[str, int]]:
    """The first ``(ip, port)`` the launched instance is reachable at."""
    for slot in service_instance.instance.uri_slot:
        for uri in slot.uri:
            if uri.ip and uri.port:
                return uri.ip, int(uri.port)
    return None


def fetch(endpoint: Tuple[str, int], working_set: int) -> bytes:
    """Wait for the service's port, then run the benchmark; the response body."""
    ip, port = endpoint
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        try:
            socket.create_connection((ip, port), timeout=5).close()
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{ip}:{port} never accepted a connection")
            time.sleep(READY_POLL_S)
    host = f"[{ip}]" if ":" in ip else ip
    url = f"http://{host}:{port}{BENCHMARK_PATH}?working_set_bytes={int(working_set)}"
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:500]!r}") from e


def stop(service_instance) -> None:
    from src.manager.manager import stop_instance

    stop_instance(token=service_instance.token)


# --- The startup step ----------------------------------------------------------------


def measure_missing(
        *,
        served: Optional[Sequence[str]] = None,
        launch: Callable = launch_local,
        get: Callable = fetch,
        release: Callable = stop,
) -> Dict[str, List[str]]:
    """Measure what is still ``-1``; returns ``{arch: keys written}``. Never raises.

    Each configured id is tried in turn until no served architecture is pending. An id
    whose declared architecture is known and already complete (or not served) is not
    launched at all.
    """
    written: Dict[str, List[str]] = {}
    try:
        ids = configured_service_ids()
        if not ids:
            return written
        pending = pending_architectures(served if served is not None else served_architectures())
        if not pending:
            return written
        if not _RUN_LOCK.acquire(blocking=False):
            log.LOGGER("[BENCHMARK] A measurement is already running; not starting another.")
            return written
    except Exception as e:
        log.LOGGER(f"[BENCHMARK] Could not decide what to measure: {e}")
        return written

    try:
        log.LOGGER(
            "[BENCHMARK] Unmeasured scores: "
            + "; ".join(f"{arch}: {', '.join(keys)}" for arch, keys in pending.items())
        )
        for service_id in ids:
            if not pending:
                break
            declared = declared_architecture(service_id)
            if declared and declared not in pending:
                continue
            arch_hint = declared or (next(iter(pending)) if len(pending) == 1 else None)
            filled = _measure_with(service_id, declared, arch_hint, pending, launch, get, release)
            if filled:
                arch, keys = filled
                written[arch] = keys
                remaining = tuple(
                    k for k in benchmark.BENCHMARK_KEYS if benchmark.node_scores(arch)[k] < 0
                )
                if remaining:
                    pending[arch] = remaining
                else:
                    pending.pop(arch, None)
        for arch, keys in pending.items():
            log.LOGGER(
                f"[BENCHMARK] {arch} still has unmeasured scores ({', '.join(keys)}); "
                "admission logs requirements on them instead of enforcing them. They are "
                "retried at the next start."
            )
    except Exception as e:
        log.LOGGER(f"[BENCHMARK] Measurement aborted: {e}")
    finally:
        _RUN_LOCK.release()
    return written


def _measure_with(service_id, declared, arch_hint, pending, launch, get, release):
    """One launch-ask-stop round with one id; ``(arch, keys written)`` or None."""
    working_set = requested_working_set(arch_hint)
    log.LOGGER(
        f"[BENCHMARK] Running benchmark service {service_id[:16]}... "
        f"(declares {declared or 'an unknown architecture'}, working set {working_set} bytes)."
    )
    try:
        instance = launch(service_id)
    except Exception as e:
        log.LOGGER(f"[BENCHMARK] Could not launch {service_id[:16]}... on this node: {e}")
        return None
    try:
        endpoint = instance_endpoint(instance)
        if endpoint is None:
            log.LOGGER(f"[BENCHMARK] {service_id[:16]}... came up with no address to ask.")
            return None
        arch, measured = parse_answer(get(endpoint, working_set))
    except Exception as e:
        log.LOGGER(f"[BENCHMARK] {service_id[:16]}... gave no usable answer: {e}")
        return None
    finally:
        try:
            release(instance)
        except Exception as e:
            log.LOGGER(f"[BENCHMARK] Could not stop the benchmark instance {instance.token}: {e}")

    if arch is None:
        log.LOGGER(f"[BENCHMARK] {service_id[:16]}... did not say which architecture it ran under.")
        return None
    if declared and arch != declared:
        log.LOGGER(
            f"[BENCHMARK] {service_id[:16]}... declares {declared} but reports {arch}; "
            "discarding its answer."
        )
        return None
    if arch not in pending:
        log.LOGGER(f"[BENCHMARK] {arch} has nothing left to measure; discarding the answer.")
        return None
    keys = write_scores(arch, measured)
    log.LOGGER(
        f"[BENCHMARK] {arch}: wrote "
        + (", ".join(f"{k}={measured[k]}" for k in keys) if keys else "nothing new")
        + " to config.yaml."
    )
    return (arch, keys) if keys else None


def start_in_background() -> Optional[threading.Thread]:
    """Run :func:`measure_missing` on a daemon thread, if there is anything to do.

    The cheap checks run here, synchronously, so a node with nothing to measure does
    not even start a thread. Never raises.
    """
    try:
        if not configured_service_ids():
            return None
        thread = threading.Thread(target=measure_missing, name="benchmark", daemon=True)
        thread.start()
        return thread
    except Exception as e:
        log.LOGGER(f"[BENCHMARK] Could not start the startup measurement: {e}")
        return None

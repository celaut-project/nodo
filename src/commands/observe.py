"""``nodo observe <instance_id>`` — real-time instance observability.

Attaches to a running local instance and continuously renders live resource
metrics (CPU / memory, current + session peak) together with a best-effort
stream of network activity involving the observed microVM.

Design notes
------------
* All the pure, side-effect-free logic (metric formatting, peak tracking,
  conntrack parsing, peer classification, trace writing) lives at module top
  level and imports only the standard library, so it can be unit-tested in
  environments that lack the nodo runtime dependencies (``bee_rpc`` etc.).
* Anything that touches the live node (cgroup / process sampling via the CH
  observability layer, protobuf metadata, the sqlite catalogue) is imported
  lazily inside the runtime functions.

Network capture is intentionally best-effort: the nodo runtime does not expose
an instance<->instance connection event bus, so we tap the Linux ``conntrack``
table (``/proc/net/nf_conntrack``) filtered by the observed VM's IP. See the
PR description for the full feasibility findings. When conntrack is not
available the command degrades cleanly with a labelled notice — it never
fabricates events.
"""

import json
import os
import signal
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.utils.config import ConfigManager

env_manager = ConfigManager()
DATABASE_FILE = env_manager.get("DATABASE_FILE")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")

CONNTRACK_PATH = "/proc/net/nf_conntrack"
REFRESH_INTERVAL_S = 1.0
MAX_EVENTS_DISPLAYED = 15


# --------------------------------------------------------------------------- #
# Pure helpers (stdlib only — unit-testable without the nodo runtime).
# --------------------------------------------------------------------------- #
def bytes_to_human(n: Optional[int]) -> str:
    """Render a byte count the same way ``nodo instances`` does."""
    if n is None:
        return "N/A"
    for threshold, unit in ((1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB")):
        if n >= threshold:
            return f"{n / threshold:.0f} {unit}"
    return f"{int(n)} B"


def short_id(instance_id: Optional[str], length: int = 8) -> str:
    if not instance_id:
        return "?"
    return str(instance_id)[:length]


def compute_cpu_percent(
    prev_usage_usec: Optional[int],
    cur_usage_usec: Optional[int],
    prev_wall_ns: Optional[int],
    cur_wall_ns: Optional[int],
) -> Optional[float]:
    """CPU% from two cgroup ``cpu.stat`` ``usage_usec`` samples.

    Returns ``None`` until a full delta is available. The value may exceed
    100% for multi-vCPU guests (it represents cumulative core usage), which
    mirrors how ``top``/cgroup accounting reports multi-core load.
    """
    if None in (prev_usage_usec, cur_usage_usec, prev_wall_ns, cur_wall_ns):
        return None
    wall_delta_us = (cur_wall_ns - prev_wall_ns) / 1000.0
    if wall_delta_us <= 0:
        return None
    usage_delta_us = cur_usage_usec - prev_usage_usec
    if usage_delta_us < 0:  # counter reset (VM restarted) — skip this sample.
        return None
    return (usage_delta_us / wall_delta_us) * 100.0


class SessionMetrics:
    """Tracks current + session-peak CPU% and memory bytes."""

    def __init__(self) -> None:
        self.cpu_current: Optional[float] = None
        self.cpu_peak: Optional[float] = None
        self.mem_current: Optional[int] = None
        self.mem_peak: Optional[int] = None

    def update_cpu(self, percent: Optional[float]) -> None:
        if percent is None:
            return
        self.cpu_current = percent
        if self.cpu_peak is None or percent > self.cpu_peak:
            self.cpu_peak = percent

    def update_memory(self, mem_bytes: Optional[int]) -> None:
        if mem_bytes is None:
            return
        self.mem_current = mem_bytes
        if self.mem_peak is None or mem_bytes > self.mem_peak:
            self.mem_peak = mem_bytes

    def cpu_str(self, value: Optional[float]) -> str:
        return "N/A" if value is None else f"{value:.0f}%"


def parse_conntrack_line(line: str, vm_ip: str) -> Optional[Dict[str, Any]]:
    """Parse a ``/proc/net/nf_conntrack`` line into a connection event.

    Only TCP/UDP flows whose *original* tuple involves ``vm_ip`` are returned.
    Direction is derived from the original (initiating) tuple:

    * original ``src == vm_ip`` → ``OUT`` (VM initiated), peer = original dst.
    * original ``dst == vm_ip`` → ``IN``  (VM is the target), peer = original src.

    Returns ``None`` for lines that do not concern the VM or are malformed.
    """
    parts = line.split()
    if len(parts) < 4:
        return None

    transport = None
    for token in parts[:4]:
        if token in ("tcp", "udp"):
            transport = token
            break
    if transport is None:
        return None

    # First occurrence of src/dst/sport/dport = the original direction tuple.
    orig: Dict[str, str] = {}
    for token in parts:
        for key in ("src", "dst", "sport", "dport"):
            if key not in orig and token.startswith(key + "="):
                orig[key] = token.split("=", 1)[1]
    if "src" not in orig or "dst" not in orig:
        return None

    def _port(value: Optional[str]) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    src_ip, dst_ip = orig["src"], orig["dst"]
    if src_ip == vm_ip:
        direction, peer_ip = "OUT", dst_ip
    elif dst_ip == vm_ip:
        direction, peer_ip = "IN", src_ip
    else:
        return None

    dst_port = _port(orig.get("dport"))
    return {
        "direction": direction,
        "transport": transport,
        "protocol": "N/A",
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": _port(orig.get("sport")),
        "dst_port": dst_port,
        "peer_ip": peer_ip,
    }


def flow_key(event: Dict[str, Any]) -> Tuple[Any, ...]:
    """Stable identity for a flow so repeated conntrack scans don't re-emit it."""
    return (
        event.get("direction"),
        event.get("transport"),
        event.get("src_ip"),
        event.get("src_port"),
        event.get("dst_ip"),
        event.get("dst_port"),
    )


def classify_peer(
    peer_ip: str,
    observed_id: str,
    observed_father_id: str,
    instance_index: Dict[str, Dict[str, str]],
) -> Dict[str, Any]:
    """Resolve a peer IP to a nodo instance (with relationship) or external host.

    ``instance_index`` maps ip → {"id", "service_id", "father_id"}.
    Relationship relative to the observed instance:

    * peer is the observed instance's father → ``parent``
    * peer's father is the observed instance   → ``child``
    * otherwise (both known nodo instances)     → ``peer``
    """
    info = instance_index.get(peer_ip)
    if not info:
        return {"kind": "external", "host": peer_ip}

    peer_id = info.get("id", "")
    if peer_id and peer_id == observed_father_id:
        relationship = "parent"
    elif info.get("father_id") and info.get("father_id") == observed_id:
        relationship = "child"
    else:
        relationship = "peer"

    return {
        "kind": "instance",
        "id": peer_id,
        "service_id": info.get("service_id", ""),
        "relationship": relationship,
    }


def format_event_line(event: Dict[str, Any], tag: Optional[str] = None) -> str:
    """Render an event to the issue's line format.

    ``12:31:04 OUT → instance c92ae2ff [gateway] (parent)``
    ``12:31:06 OUT → api.github.com  [HTTPS]``
    """
    ts = event.get("time") or ""
    direction = event.get("direction", "?")
    arrow = "→" if direction == "OUT" else "←"
    peer = event.get("peer", {})

    if peer.get("kind") == "instance":
        piece = f"instance {short_id(peer.get('id'))}"
        if tag:
            piece += f" [{tag}]"
        relationship = peer.get("relationship")
        if relationship:
            piece += f" ({relationship})"
    else:
        piece = str(peer.get("host", "?"))
        protocol = event.get("protocol")
        if protocol:
            piece += f"  [{protocol}]"

    return f"{ts} {direction:<3} {arrow} {piece}"


def serialize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten an event into a JSON-serialisable record for a ``.jsonl`` trace."""
    peer = event.get("peer", {})
    record = {
        "time": event.get("time"),
        "direction": event.get("direction"),
        "transport": event.get("transport"),
        "protocol": event.get("protocol"),
        "src": f"{event.get('src_ip')}:{event.get('src_port')}",
        "dst": f"{event.get('dst_ip')}:{event.get('dst_port')}",
    }
    if peer.get("kind") == "instance":
        record["peer_kind"] = "instance"
        record["peer_instance_id"] = peer.get("id")
        record["peer_tag"] = event.get("tag")
        record["peer_relationship"] = peer.get("relationship")
    else:
        record["peer_kind"] = "external"
        record["peer_host"] = peer.get("host")
    return record


class TraceWriter:
    """Persists observed events to a file while the session runs.

    ``.jsonl`` paths get one JSON object per line; any other suffix gets the
    human-readable event line. The file is opened lazily and flushed per event
    so a killed session still leaves a usable trace.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.jsonl = str(path).lower().endswith(".jsonl")
        self._fh = None

    def __enter__(self) -> "TraceWriter":
        self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def write(self, event: Dict[str, Any]) -> None:
        if self._fh is None:
            return
        if self.jsonl:
            self._fh.write(json.dumps(serialize_event(event)) + "\n")
        else:
            self._fh.write(format_event_line(event, tag=event.get("tag")) + "\n")
        self._fh.flush()

    def __exit__(self, *exc: Any) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------- #
# Catalogue access (sqlite — stdlib, no runtime deps).
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def _local_instances_table_exists(conn: sqlite3.Connection) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='local_instances'"
    )
    return cur.fetchone() is not None


def resolve_instance(instance_id: str) -> Optional[Dict[str, Any]]:
    """Find a local instance by exact id or unique id-prefix.

    Returns the row (id, ip, father_id, service_id, virtualizer) or ``None``.
    Raises ``ValueError`` when a prefix is ambiguous.
    """
    conn = _connect()
    try:
        if not _local_instances_table_exists(conn):
            return None
        cur = conn.execute(
            "SELECT id, ip, father_id, service_id, virtualizer "
            "FROM local_instances WHERE id = ?",
            (instance_id,),
        )
        row = cur.fetchone()
        if row is None:
            cur = conn.execute(
                "SELECT id, ip, father_id, service_id, virtualizer "
                "FROM local_instances WHERE id LIKE ?",
                (instance_id + "%",),
            )
            matches = cur.fetchall()
            if len(matches) > 1:
                raise ValueError(
                    f"Ambiguous instance id '{instance_id}' matches "
                    f"{len(matches)} instances; provide more characters."
                )
            row = matches[0] if matches else None
        return dict(row) if row is not None else None
    finally:
        conn.close()


def build_instance_index() -> Dict[str, Dict[str, str]]:
    """Map ip → {id, service_id, father_id} for all local instances."""
    index: Dict[str, Dict[str, str]] = {}
    conn = _connect()
    try:
        if not _local_instances_table_exists(conn):
            return index
        cur = conn.execute(
            "SELECT id, ip, father_id, service_id FROM local_instances"
        )
        for row in cur.fetchall():
            ip = row["ip"]
            if ip:
                index[ip] = {
                    "id": row["id"] or "",
                    "service_id": row["service_id"] or "",
                    "father_id": row["father_id"] or "",
                }
    finally:
        conn.close()
    return index


def resolve_tag(service_id: str) -> Optional[str]:
    """Resolve a service id to its first metadata tag (best-effort)."""
    if not service_id:
        return None
    try:
        from protos import celaut_pb2 as celaut

        metadata = celaut.Metadata()
        with open(os.path.join(METADATA_REGISTRY, service_id), "rb") as f:
            metadata.ParseFromString(f.read())
        if metadata.hashtag.tag:
            return metadata.hashtag.tag[0]
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# Runtime sampling (needs a live node — imported lazily).
# --------------------------------------------------------------------------- #
def _read_cgroup_cpu_usage_usec(cgroup_path: Optional[str]) -> Optional[int]:
    """Read cumulative ``usage_usec`` from a cgroup v2 ``cpu.stat`` file."""
    if not cgroup_path:
        return None
    try:
        with open(os.path.join(cgroup_path, "cpu.stat"), "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except Exception:
        return None
    return None


def _sample_resources(instance_id: str) -> Dict[str, Any]:
    """One resource sample: memory bytes + cgroup cpu usage counter."""
    from src.virtualizers.ch.observability import get_vm_runtime_snapshot

    snapshot = get_vm_runtime_snapshot(vmachine_id=instance_id)
    mem_bytes = snapshot.get("cgroup_memory_current_bytes")
    if mem_bytes is None:
        mem_bytes = snapshot.get("mem_rss_bytes")
    return {
        "alive": bool(snapshot.get("alive")),
        "mem_bytes": mem_bytes,
        "cpu_usage_usec": _read_cgroup_cpu_usage_usec(snapshot.get("cgroup_path")),
    }


def read_conntrack_events(vm_ip: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Scan the conntrack table for flows involving ``vm_ip``.

    Returns ``(events, unavailable_reason)``. ``unavailable_reason`` is a short
    label when the source can't be read (so the UI can say so honestly);
    ``None`` means conntrack was read successfully (the list may still be empty).
    """
    if not os.path.exists(CONNTRACK_PATH):
        return [], "conntrack not available (no /proc/net/nf_conntrack; run on the Linux node)"
    if not vm_ip:
        return [], "instance has no known IP; cannot correlate network flows"
    try:
        events: List[Dict[str, Any]] = []
        with open(CONNTRACK_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if vm_ip not in line:
                    continue
                event = parse_conntrack_line(line, vm_ip)
                if event:
                    events.append(event)
        return events, None
    except PermissionError:
        return [], "conntrack not readable (needs root); re-run with sudo"
    except Exception as exc:  # pragma: no cover - defensive
        return [], f"conntrack read error: {exc}"


# --------------------------------------------------------------------------- #
# Rendering + the live loop.
# --------------------------------------------------------------------------- #
def _clear_screen() -> None:
    print("\033[2J\033[H", end="")


def _render(
    header: str,
    metrics: SessionMetrics,
    events: List[str],
    save_path: Optional[str],
    net_notice: Optional[str],
) -> None:
    _clear_screen()
    lines = [f"Instance: {header}", ""]
    lines.append("CPU")
    lines.append(f" Current: {metrics.cpu_str(metrics.cpu_current)}")
    lines.append(f" Peak:    {metrics.cpu_str(metrics.cpu_peak)}")
    lines.append("")
    lines.append("Memory")
    lines.append(f" Current: {bytes_to_human(metrics.mem_current)}")
    lines.append(f" Peak:    {bytes_to_human(metrics.mem_peak)}")
    lines.append("")
    if save_path:
        lines.append(f"\033[31m●\033[0m Recording trace to {save_path}")
    lines.append("─" * 46)
    lines.append("")
    if net_notice:
        lines.append(f"(network) {net_notice}")
    if events:
        lines.extend(events[-MAX_EVENTS_DISPLAYED:])
    elif not net_notice:
        lines.append("(waiting for network activity…)")
    print("\n".join(lines), flush=True)


def observe(instance_id: str, save_path: Optional[str] = None) -> None:
    """Entry point for ``nodo observe <instance_id> [--save <path>]``."""
    try:
        instance = resolve_instance(instance_id)
    except ValueError as exc:
        print(str(exc), flush=True)
        return
    except sqlite3.Error as exc:
        print(f"Error reading instance catalogue: {exc}", flush=True)
        return

    if instance is None:
        print(
            f"No local instance found matching '{instance_id}'. "
            "Use 'nodo instances' to list running instances.",
            flush=True,
        )
        return

    full_id = instance["id"]
    father_id = instance.get("father_id") or ""
    vm_ip = instance.get("ip") or ""
    tag = resolve_tag(instance.get("service_id") or "")
    header = f"{full_id}" + (f" [{tag}]" if tag else "")

    # Validate it is actually running before attaching.
    try:
        first_sample = _sample_resources(full_id)
    except Exception as exc:
        print(f"Unable to attach to instance {short_id(full_id)}: {exc}", flush=True)
        return
    if not first_sample.get("alive"):
        print(
            f"Instance {short_id(full_id)} is not running (no live process). "
            "Nothing to observe.",
            flush=True,
        )
        return

    instance_index = build_instance_index()
    metrics = SessionMetrics()
    seen_flows: set = set()
    display_events: List[str] = []

    prev_usage = first_sample.get("cpu_usage_usec")
    prev_wall = time.monotonic_ns()
    metrics.update_memory(first_sample.get("mem_bytes"))

    stop = {"flag": False}

    def _handle_sigint(signum, frame):  # noqa: ANN001
        stop["flag"] = True

    previous_handler = signal.signal(signal.SIGINT, _handle_sigint)

    writer_cm = TraceWriter(save_path) if save_path else None
    try:
        writer = writer_cm.__enter__() if writer_cm else None
        while not stop["flag"]:
            time.sleep(REFRESH_INTERVAL_S)
            if stop["flag"]:
                break

            sample = _sample_resources(full_id)
            if not sample.get("alive"):
                _render(header, metrics, display_events, save_path,
                        "instance stopped — ending observation")
                break

            cur_wall = time.monotonic_ns()
            cur_usage = sample.get("cpu_usage_usec")
            metrics.update_cpu(
                compute_cpu_percent(prev_usage, cur_usage, prev_wall, cur_wall)
            )
            prev_usage, prev_wall = cur_usage, cur_wall
            metrics.update_memory(sample.get("mem_bytes"))

            raw_events, net_notice = read_conntrack_events(vm_ip)
            for raw in raw_events:
                key = flow_key(raw)
                if key in seen_flows:
                    continue
                seen_flows.add(key)
                peer = classify_peer(raw["peer_ip"], full_id, father_id, instance_index)
                peer_tag = (
                    resolve_tag(peer.get("service_id", ""))
                    if peer.get("kind") == "instance"
                    else None
                )
                event = {
                    **raw,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "peer": peer,
                    "tag": peer_tag,
                }
                display_events.append(format_event_line(event, tag=peer_tag))
                if writer:
                    writer.write(event)

            _render(header, metrics, display_events, save_path, net_notice)
    finally:
        if writer_cm:
            writer_cm.__exit__(None, None, None)
        signal.signal(signal.SIGINT, previous_handler)

    print(f"\nObservation of {short_id(full_id)} ended.", flush=True)

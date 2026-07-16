"""``nodo observe <instance_id>`` — real-time instance observability.

Attaches to a running local instance and continuously renders live resource
metrics (CPU / memory, current + session peak) together with a live stream of
the microVM's network activity.

Design notes
------------
* All the pure, side-effect-free logic (metric formatting, peak tracking, pcap
  header framing, ethernet/IP/TCP-UDP parsing, peer classification, save-dir
  path building, jsonl/pcap writers) lives at module top level and imports only
  the standard library, so it can be unit-tested in environments that lack the
  nodo runtime dependencies (``bee_rpc`` etc.) and without a live VM or root.
* Anything that touches the live node (cgroup / process sampling via the CH
  observability layer, protobuf metadata, the sqlite catalogue, the AF_PACKET
  raw socket) is imported / opened lazily inside the runtime functions.

Network capture
---------------
When running on the Linux/KVM host with ``CAP_NET_RAW``, we bind an AF_PACKET
raw socket to the microVM's *tap* interface and capture **every** ethernet
frame in both directions — the Wireshark-equivalent of the VM's whole NIC. The
tap name is a deterministic function of the instance id (see
``tap_ifname_for_instance``), matching how the CH virtualizer creates it, so no
extra catalogue lookup is needed. Transport protocol, ports, TCP flags and
direction are derived from the real IP/TCP/UDP headers — there is no
port→app-name guessing.

If AF_PACKET is unavailable (non-root, non-Linux, or the tap can't be found)
the command degrades to the legacy ``conntrack`` table scan for the on-screen
feed and clearly labels the degraded mode. It never fabricates events, and in
degraded mode no ``.pcap`` is written.
"""

import json
import os
import select
import signal
import socket
import struct
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

# libpcap framing constants.
PCAP_MAGIC = 0xA1B2C3D4
PCAP_VERSION_MAJOR = 2
PCAP_VERSION_MINOR = 4
LINKTYPE_ETHERNET = 1          # tap devices created in "tap" mode carry L2 frames.
DEFAULT_SNAPLEN = 65535        # capture whole frames (jumbo-safe, Wireshark default).
ETH_P_ALL = 0x0003             # AF_PACKET protocol: every frame, both directions.

# Save-directory file names (documented convention — see PR / USAGE).
METRICS_FILENAME = "metrics.jsonl"
CAPTURE_FILENAME = "capture.pcap"

# IP protocol byte → transport label (RFC 790). Derived from the IP header, not
# from a port→app map.
IP_PROTO = {
    1: "icmp",
    6: "tcp",
    17: "udp",
    58: "icmpv6",
}


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


# --------------------------------------------------------------------------- #
# Tap interface resolution.
# --------------------------------------------------------------------------- #
def tap_ifname_for_instance(instance_id: str) -> str:
    """Return the tap interface name the CH virtualizer creates for ``instance_id``.

    Mirrors ``src/virtualizers/ch/execute.py::_create_tap`` exactly:
    ``tap`` + the first 10 hex chars of ``sha1(instance_id)``. Keeping this a
    pure re-derivation (rather than a new catalogue column) means observe stays
    read-only and never diverges from the value the runtime actually programmed.
    """
    import hashlib

    tap_suffix = hashlib.sha1(instance_id.encode("utf-8")).hexdigest()[:10]
    return f"tap{tap_suffix}"


def interface_exists(ifname: str) -> bool:
    """True when a host network interface with this name is present."""
    return bool(ifname) and os.path.isdir(os.path.join("/sys/class/net", ifname))


# --------------------------------------------------------------------------- #
# pcap framing (stdlib struct — no library, openable in Wireshark).
# --------------------------------------------------------------------------- #
def pcap_global_header(snaplen: int = DEFAULT_SNAPLEN,
                       network: int = LINKTYPE_ETHERNET) -> bytes:
    """24-byte libpcap global header (little-endian, microsecond magic)."""
    return struct.pack(
        "<IHHiIII",
        PCAP_MAGIC,
        PCAP_VERSION_MAJOR,
        PCAP_VERSION_MINOR,
        0,          # thiszone (GMT to local correction)
        0,          # sigfigs
        snaplen,
        network,
    )


def pcap_packet_header(ts_sec: int, ts_usec: int,
                       incl_len: int, orig_len: int) -> bytes:
    """16-byte libpcap per-record header."""
    return struct.pack("<IIII", ts_sec, ts_usec, incl_len, orig_len)


def pcap_record(frame: bytes, timestamp: float,
                snaplen: int = DEFAULT_SNAPLEN) -> bytes:
    """A full pcap record (header + possibly-truncated frame) for one frame."""
    ts_sec = int(timestamp)
    ts_usec = int(round((timestamp - ts_sec) * 1_000_000))
    if ts_usec >= 1_000_000:  # rounding spilled into the next second
        ts_sec += 1
        ts_usec -= 1_000_000
    orig_len = len(frame)
    incl = frame[:snaplen]
    return pcap_packet_header(ts_sec, ts_usec, len(incl), orig_len) + incl


# --------------------------------------------------------------------------- #
# Ethernet / IPv4 / TCP-UDP header parsing (protocol + direction, no port map).
# --------------------------------------------------------------------------- #
def _ipv4_str(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


def tcp_flags_str(flags: Optional[int]) -> Optional[str]:
    """Decode the TCP flag byte into a compact string like ``SYN,ACK``."""
    if flags is None:
        return None
    names = [
        (0x01, "FIN"), (0x02, "SYN"), (0x04, "RST"),
        (0x08, "PSH"), (0x10, "ACK"), (0x20, "URG"),
    ]
    active = [name for bit, name in names if flags & bit]
    return ",".join(active) if active else None


def parse_ethernet_frame(frame: bytes, vm_ip: str) -> Optional[Dict[str, Any]]:
    """Parse a raw ethernet frame into a connection event relative to ``vm_ip``.

    Only IPv4 TCP/UDP/ICMP frames that involve ``vm_ip`` are surfaced for the
    live feed (every frame is still written to the pcap verbatim). Direction is
    derived purely from the IP header:

    * IP ``src == vm_ip`` → ``OUT`` (VM initiated), peer = IP dst.
    * IP ``dst == vm_ip`` → ``IN``  (VM is the target), peer = IP src.

    Transport comes from the IP protocol byte; ports/flags from the L4 header.
    Returns ``None`` for non-IPv4, non-VM, or malformed frames.
    """
    if len(frame) < 14:
        return None
    ethertype = int.from_bytes(frame[12:14], "big")
    if ethertype != 0x0800:  # IPv4 only for the live feed.
        return None
    return _parse_ipv4(frame[14:], vm_ip)


def _parse_ipv4(payload: bytes, vm_ip: str) -> Optional[Dict[str, Any]]:
    if len(payload) < 20:
        return None
    ver_ihl = payload[0]
    if (ver_ihl >> 4) != 4:
        return None
    ihl = (ver_ihl & 0x0F) * 4
    if ihl < 20 or len(payload) < ihl:
        return None
    proto_byte = payload[9]
    src_ip = _ipv4_str(payload[12:16])
    dst_ip = _ipv4_str(payload[16:20])
    transport = IP_PROTO.get(proto_byte, f"ip-proto-{proto_byte}")

    src_port: Optional[int] = None
    dst_port: Optional[int] = None
    flags: Optional[int] = None
    l4 = payload[ihl:]
    if proto_byte in (6, 17) and len(l4) >= 4:
        src_port = int.from_bytes(l4[0:2], "big")
        dst_port = int.from_bytes(l4[2:4], "big")
        if proto_byte == 6 and len(l4) >= 14:
            flags = l4[13]

    if src_ip == vm_ip:
        direction, peer_ip = "OUT", dst_ip
    elif dst_ip == vm_ip:
        direction, peer_ip = "IN", src_ip
    else:
        return None

    return {
        "direction": direction,
        "transport": transport,
        "protocol": transport.upper(),   # display label = transport, no app guess.
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": src_port,
        "dst_port": dst_port,
        "tcp_flags": tcp_flags_str(flags),
        "peer_ip": peer_ip,
    }


# --------------------------------------------------------------------------- #
# Legacy conntrack parsing (fallback source for the live feed only).
# --------------------------------------------------------------------------- #
def parse_conntrack_line(line: str, vm_ip: str) -> Optional[Dict[str, Any]]:
    """Parse a ``/proc/net/nf_conntrack`` line into a connection event.

    Fallback path used when AF_PACKET capture isn't available. Transport comes
    from the conntrack ``tcp``/``udp`` token; there is no app-protocol guessing.
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

    return {
        "direction": direction,
        "transport": transport,
        "protocol": transport.upper(),
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": _port(orig.get("sport")),
        "dst_port": _port(orig.get("dport")),
        "tcp_flags": None,
        "peer_ip": peer_ip,
    }


def flow_key(event: Dict[str, Any]) -> Tuple[Any, ...]:
    """Stable identity for a flow so repeated packets/scans don't re-emit it."""
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
    ``12:31:06 OUT → 34.117.5.5  [TCP]``
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
    """Flatten an event into a JSON-serialisable record (used by any jsonl trace)."""
    peer = event.get("peer", {})
    record = {
        "time": event.get("time"),
        "direction": event.get("direction"),
        "transport": event.get("transport"),
        "protocol": event.get("protocol"),
        "tcp_flags": event.get("tcp_flags"),
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


# --------------------------------------------------------------------------- #
# Save directory + writers (jsonl metrics + pcap capture).
# --------------------------------------------------------------------------- #
def _sanitize_component(name: str) -> str:
    """Make a tag safe to use as a single path component."""
    keep = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        else:
            keep.append("-")
    cleaned = "".join(keep).strip("-.")
    return cleaned


def build_save_dir(base_path: str, instance_name: Optional[str],
                   instance_id: str) -> str:
    """Directory path for a saved session: ``<base>/<name>_<id>`` (or ``<id>``).

    ``instance_name`` is the resolved metadata tag when present. When there is
    no tag the directory is just the (full) instance id.
    """
    if instance_name:
        safe = _sanitize_component(instance_name)
        dirname = f"{safe}_{instance_id}" if safe else instance_id
    else:
        dirname = instance_id
    return os.path.join(base_path, dirname)


def metrics_record(metrics: "SessionMetrics", alive: bool,
                   timestamp: Optional[str] = None) -> Dict[str, Any]:
    """One CPU+memory sample as a jsonl-ready dict (mirrors the live panel)."""
    return {
        "time": timestamp if timestamp is not None
        else datetime.now().strftime("%H:%M:%S"),
        "alive": alive,
        "cpu_percent": (None if metrics.cpu_current is None
                        else round(metrics.cpu_current, 1)),
        "cpu_peak_percent": (None if metrics.cpu_peak is None
                             else round(metrics.cpu_peak, 1)),
        "mem_bytes": metrics.mem_current,
        "mem_peak_bytes": metrics.mem_peak,
    }


class MetricsWriter:
    """Appends CPU+memory samples to a ``.jsonl`` file, flushed per sample."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None

    def __enter__(self) -> "MetricsWriter":
        self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def write(self, record: Dict[str, Any]) -> None:
        if self._fh is None:
            return
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def __exit__(self, *exc: Any) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class PcapWriter:
    """Writes captured frames to a standard libpcap file, flushed per frame."""

    def __init__(self, path: str, snaplen: int = DEFAULT_SNAPLEN,
                 network: int = LINKTYPE_ETHERNET) -> None:
        self.path = path
        self.snaplen = snaplen
        self.network = network
        self._fh = None

    def __enter__(self) -> "PcapWriter":
        self._fh = open(self.path, "wb")
        self._fh.write(pcap_global_header(self.snaplen, self.network))
        self._fh.flush()
        return self

    def write_frame(self, frame: bytes, timestamp: float) -> None:
        if self._fh is None:
            return
        self._fh.write(pcap_record(frame, timestamp, self.snaplen))
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


def capture_available() -> bool:
    """True when AF_PACKET raw sockets are usable on this platform."""
    return hasattr(socket, "AF_PACKET")


def open_packet_socket(tap_ifname: str, snaplen: int = DEFAULT_SNAPLEN):
    """Bind an AF_PACKET raw socket to ``tap_ifname`` (Linux + CAP_NET_RAW).

    Returns a non-blocking socket, or raises the underlying OSError (permission,
    missing interface, unsupported platform) so the caller can degrade cleanly.
    """
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    try:
        # Best-effort larger receive buffer so bursts aren't dropped between the
        # per-second display ticks.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    s.bind((tap_ifname, 0))
    s.setblocking(False)
    return s


def resolve_capture_source(instance_id: str, vm_ip: str) -> Tuple[Optional[str], Optional[str]]:
    """Decide how to capture: ``(tap_ifname, degraded_reason)``.

    Returns the tap interface name when AF_PACKET capture is viable, otherwise
    ``(None, reason)`` describing why we fall back to conntrack.
    """
    if not capture_available():
        return None, ("AF_PACKET unavailable (non-Linux host); "
                      "falling back to conntrack, no pcap will be written")
    tap = tap_ifname_for_instance(instance_id)
    if not interface_exists(tap):
        return None, (f"tap interface '{tap}' not found under /sys/class/net; "
                      "falling back to conntrack, no pcap will be written")
    return tap, None


def read_conntrack_events(vm_ip: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Scan the conntrack table for flows involving ``vm_ip`` (fallback source).

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
    save_dir: Optional[str],
    net_notice: Optional[str],
    capture_mode: str,
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
    if save_dir:
        lines.append(f"\033[31m●\033[0m Recording to {save_dir}/")
        lines.append(f"    ├─ {METRICS_FILENAME}   (cpu+memory samples)")
        pcap_note = CAPTURE_FILENAME if capture_mode == "pcap" else \
            f"{CAPTURE_FILENAME} — not written (degraded: conntrack mode)"
        lines.append(f"    └─ {pcap_note}")
    lines.append("─" * 46)
    lines.append(f"network: {capture_mode}")
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

    # Decide capture source (AF_PACKET on the tap, else conntrack fallback).
    tap_ifname, degraded_reason = resolve_capture_source(full_id, vm_ip)
    pcap_sock = None
    capture_mode = "conntrack"
    if tap_ifname:
        try:
            pcap_sock = open_packet_socket(tap_ifname)
            capture_mode = "pcap"
        except OSError as exc:
            degraded_reason = (
                f"AF_PACKET bind to '{tap_ifname}' failed ({exc}); "
                "falling back to conntrack, no pcap will be written"
            )
            pcap_sock = None

    # Set up the save directory + writers.
    save_dir: Optional[str] = None
    metrics_writer_cm: Optional[MetricsWriter] = None
    pcap_writer_cm: Optional[PcapWriter] = None
    if save_path:
        save_dir = build_save_dir(save_path, tag, full_id)
        os.makedirs(save_dir, exist_ok=True)
        metrics_writer_cm = MetricsWriter(os.path.join(save_dir, METRICS_FILENAME))
        if capture_mode == "pcap":
            pcap_writer_cm = PcapWriter(os.path.join(save_dir, CAPTURE_FILENAME))

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

    def _emit(raw: Dict[str, Any], net_writer) -> None:
        """Classify + display (once per flow) a parsed connection event."""
        key = flow_key(raw)
        if key in seen_flows:
            return
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

    try:
        metrics_writer = metrics_writer_cm.__enter__() if metrics_writer_cm else None
        pcap_writer = pcap_writer_cm.__enter__() if pcap_writer_cm else None

        next_sample = time.monotonic()
        while not stop["flag"]:
            now = time.monotonic()
            timeout = max(0.0, next_sample - now)

            # --- drain network activity --------------------------------------
            if pcap_sock is not None:
                ready, _, _ = select.select([pcap_sock], [], [], timeout)
                if ready:
                    while True:
                        try:
                            frame = pcap_sock.recv(DEFAULT_SNAPLEN)
                        except BlockingIOError:
                            break
                        except OSError:
                            break
                        ts = time.time()
                        if pcap_writer:
                            pcap_writer.write_frame(frame, ts)
                        parsed = parse_ethernet_frame(frame, vm_ip)
                        if parsed:
                            _emit(parsed, None)
            else:
                if timeout > 0:
                    time.sleep(timeout)

            if time.monotonic() < next_sample:
                # Woke early for packets — loop again before sampling metrics.
                continue
            next_sample += REFRESH_INTERVAL_S

            # --- sample resource metrics -------------------------------------
            sample = _sample_resources(full_id)
            if not sample.get("alive"):
                _render(header, metrics, display_events, save_dir,
                        "instance stopped — ending observation", capture_mode)
                break

            cur_wall = time.monotonic_ns()
            cur_usage = sample.get("cpu_usage_usec")
            metrics.update_cpu(
                compute_cpu_percent(prev_usage, cur_usage, prev_wall, cur_wall)
            )
            prev_usage, prev_wall = cur_usage, cur_wall
            metrics.update_memory(sample.get("mem_bytes"))

            if metrics_writer:
                metrics_writer.write(metrics_record(metrics, alive=True))

            # In conntrack fallback mode, scan the table each tick.
            net_notice = degraded_reason
            if pcap_sock is None:
                raw_events, conntrack_notice = read_conntrack_events(vm_ip)
                for raw in raw_events:
                    _emit(raw, None)
                if conntrack_notice:
                    net_notice = conntrack_notice

            _render(header, metrics, display_events, save_dir,
                    net_notice, capture_mode)
    finally:
        if pcap_writer_cm:
            pcap_writer_cm.__exit__(None, None, None)
        if metrics_writer_cm:
            metrics_writer_cm.__exit__(None, None, None)
        if pcap_sock is not None:
            try:
                pcap_sock.close()
            except OSError:
                pass
        signal.signal(signal.SIGINT, previous_handler)

    print(f"\nObservation of {short_id(full_id)} ended.", flush=True)

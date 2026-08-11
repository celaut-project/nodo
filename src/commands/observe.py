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

The on-screen network section is a **live per-flow table** (``FlowTable``): each
flow (direction+transport+addrs+ports) is one row that accumulates packet and
byte counters and a last-seen timestamp, so a chatty connection visibly ticks up
next to the CPU/memory numbers instead of printing once and looking frozen. The
display re-renders on network bursts (throttled) as well as on the 1 s metrics
tick, and CPU/memory + the network table always appear in the *same* frame. The
``.pcap`` still records **every** frame verbatim; the aggregation is display-only.

Packet timestamps come from the kernel (``SO_TIMESTAMPNS`` / ``SCM_TIMESTAMPNS``
ancillary data on ``recvmsg``) for accurate inter-packet timing in the pcap,
falling back to ``time.time()`` when the ancillary stamp is unavailable. The pcap
link-type is auto-detected from the interface (``/sys/class/net/<if>/type``):
ARPHRD_ETHER → ``LINKTYPE_ETHERNET``, a tun/ARPHRD_NONE device → ``LINKTYPE_RAW``.

If AF_PACKET is unavailable (non-root, non-Linux, or the tap can't be found)
the command degrades: it tries the legacy ``conntrack`` table scan for the
on-screen feed and clearly labels the degraded mode. That scan reads
``/proc/net/nf_conntrack``, which **current Ubuntu kernels do not expose**
(``CONFIG_NF_CONNTRACK_PROCFS`` is unset), so on those hosts there is no per-flow
fallback at all — see ``conntrack_unavailable_reason``, which reports that as the
kernel build option it is rather than as "you are not on the node".

What survives every degradation is the **network volume** panel: the host tap's
cumulative byte/packet counters from ``/sys/class/net/<tap>/statistics``, which
need neither ``CAP_NET_RAW`` nor conntrack. Per-flow detail needs the capture.

It never fabricates events, and in degraded mode no ``.pcap`` is written — when
``--save`` is set a ``capture_unavailable.txt`` note is written into the save dir
explaining why, so the artifact folder is self-explanatory.
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
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from src.utils.config import ConfigManager

env_manager = ConfigManager()
DATABASE_FILE = env_manager.get("DATABASE_FILE")
METADATA_REGISTRY = env_manager.get("METADATA_REGISTRY")

CONNTRACK_PATH = "/proc/net/nf_conntrack"
# Present whenever nf_conntrack is loaded, regardless of CONFIG_NF_CONNTRACK_PROCFS.
# Ubuntu ships that (deprecated) procfs interface disabled, so CONNTRACK_PATH being
# absent says nothing about whether the kernel is tracking flows — this does.
CONNTRACK_COUNT_PATH = "/proc/sys/net/netfilter/nf_conntrack_count"
REFRESH_INTERVAL_S = 1.0
# Min wall-time between renders triggered by network bursts, so a chatty flow
# doesn't flicker the screen faster than the eye can read. Metrics still sample
# on their own REFRESH_INTERVAL_S cadence.
MIN_RENDER_INTERVAL_S = 0.2
MAX_EVENTS_DISPLAYED = 15

# libpcap framing constants.
PCAP_MAGIC = 0xA1B2C3D4
PCAP_VERSION_MAJOR = 2
PCAP_VERSION_MINOR = 4
LINKTYPE_ETHERNET = 1          # ARPHRD_ETHER tap: L2 ethernet frames.
LINKTYPE_RAW = 101             # tun/ARPHRD_NONE: raw IP, no ethernet header.
DEFAULT_SNAPLEN = 65535        # capture whole frames (jumbo-safe, Wireshark default).
ETH_P_ALL = 0x0003             # AF_PACKET protocol: every frame, both directions.

# Linux ARPHRD_* interface hardware types (from /sys/class/net/<if>/type).
ARPHRD_ETHER = 1
ARPHRD_NONE = 0xFFFE           # 65534 — tun / point-to-point raw-IP devices.

# Kernel receive-timestamp options. getattr fallbacks keep the module importable
# on platforms whose ``socket`` lacks the constants (e.g. macOS dev boxes); the
# values are only ever used on the Linux AF_PACKET path.
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
SCM_TIMESTAMPNS = getattr(socket, "SCM_TIMESTAMPNS", SO_TIMESTAMPNS)
# struct timespec { long tv_sec; long tv_nsec; } in native size/alignment.
_TIMESPEC_FMT = "@ll"

# Save-directory file names (documented convention — see PR / USAGE).
METRICS_FILENAME = "metrics.jsonl"
CAPTURE_FILENAME = "capture.pcap"
CAPTURE_UNAVAILABLE_FILENAME = "capture_unavailable.txt"

# IP protocol byte → transport label (RFC 790). Derived from the IP header, not
# from a port→app map.
IP_PROTO = {
    1: "icmp",
    6: "tcp",
    17: "udp",
    58: "icmpv6",
}


class ObserveInstanceError(Exception):
    """Raised when an instance can't be observed (not found / not running).

    Carries a human-facing ``message`` so both front-ends report the same
    reason: the CLI prints it, the ``Gateway.Observe`` RPC surfaces it as a
    trailing ``notice`` event.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


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


def human_bytes_1dp(n: Optional[int]) -> str:
    """Like :func:`bytes_to_human` but with one decimal (``38.4 KB``).

    Used for the live per-flow byte totals where a single decimal makes the
    tick-up between renders visible.
    """
    if n is None:
        return "N/A"
    for threshold, unit in ((1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "KB")):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{int(n)} B"


def short_id(instance_id: Optional[str], length: int = 8) -> str:
    if not instance_id:
        return "?"
    return str(instance_id)[:length]


def format_balance(balance: Any) -> str:
    """Render an instance's balance in ERG, the way ``nodo instances`` does.

    A balance is stored in the catalogue as a numeric string of MU (celaut ``Amount``).
    We render it through the node's ERG formatter when it is importable and degrade
    gracefully otherwise, so this stays unit-testable without the nodo runtime.
    """
    if balance is None or balance == "":
        return "N/A"
    try:
        balance_mu = int(balance)
    except (ValueError, TypeError):
        return "Invalid balance"
    try:
        from src.utils.monetary import format_mu

        return f"{format_mu(balance_mu)}"
    except Exception:
        return str(balance_mu)


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
        # Latest cumulative I/O counters (disk block-IO + tap net), or None.
        self.disk_read_bytes: Optional[int] = None
        self.disk_write_bytes: Optional[int] = None
        self.net_rx_bytes: Optional[int] = None
        self.net_tx_bytes: Optional[int] = None
        self.net_rx_packets: Optional[int] = None
        self.net_tx_packets: Optional[int] = None

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

    def update_io(self, sample: Dict[str, Any]) -> None:
        """Store the latest cumulative disk/net counters (None-safe)."""
        for attr in ("disk_read_bytes", "disk_write_bytes",
                     "net_rx_bytes", "net_tx_bytes",
                     "net_rx_packets", "net_tx_packets"):
            value = sample.get(attr)
            if value is not None:
                setattr(self, attr, value)

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


def read_interface_arptype(ifname: str) -> Optional[int]:
    """Read the ARPHRD hardware type from ``/sys/class/net/<ifname>/type``.

    Returns the integer type (1 = ARPHRD_ETHER, 65534 = ARPHRD_NONE/tun) or
    ``None`` when it can't be read (non-Linux, missing iface).
    """
    try:
        with open(os.path.join("/sys/class/net", ifname, "type"),
                  "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def frame_looks_ethernet(frame: bytes) -> bool:
    """Heuristic: does ``frame`` start with an ethernet header vs a raw IP packet?

    Used as a fallback when the sysfs ARPHRD type is unavailable. A raw IPv4/IPv6
    packet begins with a version nibble of 4 or 6; a real ethernet frame's first
    byte is a MAC octet and bytes 12-13 hold a known ethertype.
    """
    if len(frame) >= 14:
        ethertype = int.from_bytes(frame[12:14], "big")
        if ethertype in (0x0800, 0x0806, 0x86DD):  # IPv4, ARP, IPv6
            return True
    if frame and (frame[0] >> 4) in (4, 6):
        return False
    return True  # default to ethernet (cloud-hypervisor uses a tap/L2 device).


def detect_link_type(ifname: str,
                     first_frame: Optional[bytes] = None) -> Tuple[int, bool]:
    """Pick the pcap link-type for ``ifname``: ``(linktype, is_ethernet)``.

    Primary signal is the sysfs ARPHRD type; ``ARPHRD_NONE`` (tun) means the
    device delivers raw IP → ``LINKTYPE_RAW`` and no 14-byte ethernet header to
    strip. If sysfs is unreadable but a sniffed ``first_frame`` is supplied, fall
    back to :func:`frame_looks_ethernet`. Default (and cloud-hypervisor's tap)
    stays Ethernet — but no longer blindly.
    """
    arptype = read_interface_arptype(ifname)
    if arptype == ARPHRD_ETHER:
        return LINKTYPE_ETHERNET, True
    if arptype == ARPHRD_NONE:
        return LINKTYPE_RAW, False
    if arptype is None and first_frame is not None:
        if frame_looks_ethernet(first_frame):
            return LINKTYPE_ETHERNET, True
        return LINKTYPE_RAW, False
    return LINKTYPE_ETHERNET, True


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


def parse_scm_timestampns(ancdata: List[Tuple[int, int, bytes]]) -> Optional[float]:
    """Extract the kernel receive time (float seconds) from ``recvmsg`` ancillary.

    Looks for a ``SOL_SOCKET``/``SCM_TIMESTAMPNS`` control message carrying a
    ``struct timespec`` (tv_sec, tv_nsec). Returns ``None`` when no usable
    timestamp cmsg is present so callers can fall back to ``time.time()``.
    """
    size = struct.calcsize(_TIMESPEC_FMT)
    for level, ctype, cdata in ancdata:
        if level == socket.SOL_SOCKET and ctype == SCM_TIMESTAMPNS and len(cdata) >= size:
            tv_sec, tv_nsec = struct.unpack(_TIMESPEC_FMT, cdata[:size])
            return tv_sec + tv_nsec / 1_000_000_000.0
    return None


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


def parse_ethernet_frame(frame: bytes, vm_ip: str,
                         is_ethernet: bool = True) -> Optional[Dict[str, Any]]:
    """Parse a captured frame into a connection event relative to ``vm_ip``.

    ``is_ethernet`` selects the link layer:

    * ``True``  (LINKTYPE_ETHERNET / tap) — strip the 14-byte ethernet header
      and require an IPv4 ethertype.
    * ``False`` (LINKTYPE_RAW / tun)      — the frame *is* the IP packet.

    Only IPv4 TCP/UDP/ICMP frames that involve ``vm_ip`` are surfaced for the
    live feed (every frame is still written to the pcap verbatim). Direction is
    derived purely from the IP header:

    * IP ``src == vm_ip`` → ``OUT`` (VM initiated), peer = IP dst.
    * IP ``dst == vm_ip`` → ``IN``  (VM is the target), peer = IP src.

    Transport comes from the IP protocol byte; ports/flags from the L4 header.
    Returns ``None`` for non-IPv4, non-VM, or malformed frames.
    """
    if not is_ethernet:  # raw IP: no ethernet header to skip.
        return _parse_ipv4(frame, vm_ip)
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
# Live per-flow aggregation (what the network panel shows — the pcap keeps
# every frame; this is display-only state).
# --------------------------------------------------------------------------- #
_PEER_COL_WIDTH = 40   # truncate long peer strings so rows stay one line.


class FlowTable:
    """Ordered table of active flows, one row per :func:`flow_key`.

    Each packet *updates* its flow's counters (packet count, byte total,
    last-seen) instead of being suppressed after the first sighting, so a busy
    connection visibly ticks up on screen. Rows are surfaced newest-activity
    first (sorted by last-seen descending) and capped to ``max_rows``.
    """

    def __init__(self, max_rows: int = MAX_EVENTS_DISPLAYED) -> None:
        self.max_rows = max_rows
        self._flows: "Dict[Tuple[Any, ...], Dict[str, Any]]" = {}

    def get(self, key: Tuple[Any, ...]) -> Optional[Dict[str, Any]]:
        return self._flows.get(key)

    def update(self, key: Tuple[Any, ...], *, direction: str,
               protocol: Optional[str], peer: Dict[str, Any], tag: Optional[str],
               frame_len: Optional[int], timestamp: float,
               source: str) -> Dict[str, Any]:
        """Create or update the row for ``key`` and return it.

        ``frame_len`` is the captured frame length (added to the byte total) for
        the AF_PACKET path, or ``None`` for the conntrack fallback where per-flow
        byte counts aren't available. ``source`` is ``"pcap"`` or ``"conntrack"``.
        """
        row = self._flows.get(key)
        if row is None:
            row = {
                "direction": direction,
                "protocol": protocol,
                "peer": peer,
                "tag": tag,
                "packets": 0,
                "bytes": 0,
                "first_seen": timestamp,
                "last_seen": timestamp,
                "source": source,
            }
            self._flows[key] = row
        row["packets"] += 1
        if frame_len is not None:
            row["bytes"] += frame_len
        row["last_seen"] = timestamp
        return row

    def active_rows(self) -> List[Dict[str, Any]]:
        """Most-recently-active flows first, capped to ``max_rows``."""
        rows = sorted(self._flows.values(),
                      key=lambda r: r["last_seen"], reverse=True)
        return rows[:self.max_rows]


def format_flow_line(row: Dict[str, Any]) -> str:
    """Render one :class:`FlowTable` row as a live-feed line.

    ``12:31:07  OUT → instance c92ae2ff [gateway] (parent)      TCP    142 pkts   38.4 KB``

    Newest activity is shown at the top of the panel. In conntrack fallback mode
    per-flow byte counts aren't available, so the size column reads ``conntrack``.
    """
    ts = time.strftime("%H:%M:%S", time.localtime(row["last_seen"]))
    direction = row.get("direction", "?")
    arrow = "→" if direction == "OUT" else "←"

    peer = row.get("peer", {})
    if peer.get("kind") == "instance":
        piece = f"instance {short_id(peer.get('id'))}"
        if row.get("tag"):
            piece += f" [{row['tag']}]"
        relationship = peer.get("relationship")
        if relationship:
            piece += f" ({relationship})"
    else:
        piece = str(peer.get("host", "?"))
    if len(piece) > _PEER_COL_WIDTH:
        piece = piece[:_PEER_COL_WIDTH - 1] + "…"

    protocol = row.get("protocol") or "?"
    packets = row.get("packets", 0)
    if row.get("source") == "conntrack":
        size = "conntrack"
    else:
        size = human_bytes_1dp(row.get("bytes", 0))

    return (f"{ts}  {direction:<3} {arrow} {piece:<{_PEER_COL_WIDTH}} "
            f"{protocol:<5} {packets:>5} pkts  {size:>9}")


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


def write_capture_unavailable_note(save_dir: str, reason: str) -> str:
    """Write ``capture_unavailable.txt`` explaining why no pcap was produced.

    Keeps a ``--save`` artifact folder self-explanatory when capture degraded to
    conntrack (no CAP_NET_RAW / non-Linux / tap missing / bind failed). Returns
    the path written.
    """
    path = os.path.join(save_dir, CAPTURE_UNAVAILABLE_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("No packet capture (.pcap) was produced for this session.\n\n")
        f.write(f"Reason: {reason}\n\n")
        f.write("The metrics.jsonl file still contains the CPU/memory samples. "
                "To capture packets, re-run `nodo observe` on the Linux/KVM host "
                "with CAP_NET_RAW (e.g. via sudo) so the AF_PACKET tap capture "
                "can bind.\n")
    return path


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
        "disk_read_bytes": metrics.disk_read_bytes,
        "disk_write_bytes": metrics.disk_write_bytes,
        "net_rx_bytes": metrics.net_rx_bytes,
        "net_tx_bytes": metrics.net_tx_bytes,
        "net_rx_packets": metrics.net_rx_packets,
        "net_tx_packets": metrics.net_tx_packets,
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
    """Find a local instance by exact instance id.

    Returns the row (id, ip, father_id, service_id, virtualizer, name, balance_mu) or
    ``None``. Matching is exact (``WHERE id = ?``); no prefix resolution.
    """
    conn = _connect()
    try:
        if not _local_instances_table_exists(conn):
            return None
        cur = conn.execute(
            "SELECT id, ip, father_id, service_id, virtualizer, name, balance_mu "
            "FROM local_instances WHERE id = ?",
            (instance_id,),
        )
        row = cur.fetchone()
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


def read_instance_balance(instance_id: str) -> Optional[str]:
    """Best-effort read of an instance's current balance from the catalogue.

    Returns the raw numeric-string value (or ``None`` when unknown) so the live
    panel can reflect the balance being spent while the instance runs. Never raises.
    """
    conn = _connect()
    try:
        if not _local_instances_table_exists(conn):
            return None
        cur = conn.execute(
            "SELECT balance_mu FROM local_instances WHERE id = ?", (instance_id,)
        )
        row = cur.fetchone()
        return row["balance_mu"] if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _resolve_service_tag(service_id: str) -> Optional[str]:
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
        "disk_read_bytes": snapshot.get("disk_read_bytes"),
        "disk_write_bytes": snapshot.get("disk_write_bytes"),
        "net_rx_bytes": snapshot.get("net_rx_bytes"),
        "net_tx_bytes": snapshot.get("net_tx_bytes"),
        "net_rx_packets": snapshot.get("net_rx_packets"),
        "net_tx_packets": snapshot.get("net_tx_packets"),
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
    try:
        # Ask the kernel to attach a nanosecond receive timestamp to each packet
        # (read via recvmsg ancillary data) for accurate inter-packet pcap timing.
        s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
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


def conntrack_unavailable_reason() -> Optional[str]:
    """Why the per-flow conntrack table can't be read here, or ``None`` if it can.

    ``/proc/net/nf_conntrack`` missing has three different causes and only one of
    them is "you're not on the node": Ubuntu builds its kernels with
    ``CONFIG_NF_CONNTRACK_PROCFS`` unset, so conntrack can be loaded and tracking
    hundreds of flows while the file does not exist. Reporting that as "run on the
    Linux node" sends the operator after a fix that cannot work.
    """
    if os.path.exists(CONNTRACK_PATH):
        return None
    if not capture_available():
        return "conntrack unavailable: not a Linux host (run this on the node)"
    if os.path.exists(CONNTRACK_COUNT_PATH):
        return ("per-flow table unavailable: kernel built without "
                "CONFIG_NF_CONNTRACK_PROCFS (normal on Ubuntu) — the network "
                "volume panel is still live")
    return "per-flow table unavailable: nf_conntrack not loaded on this host"


def read_conntrack_events(vm_ip: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Scan the conntrack table for flows involving ``vm_ip`` (fallback source).

    Returns ``(events, unavailable_reason)``. ``unavailable_reason`` is a short
    label when the source can't be read (so the UI can say so honestly);
    ``None`` means conntrack was read successfully (the list may still be empty).
    """
    missing = conntrack_unavailable_reason()
    if missing:
        return [], missing
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
    balance: str,
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
    # Cumulative tap counters. Already sampled every tick for metrics.jsonl and
    # previously never shown: this is the one network reading that needs neither
    # CAP_NET_RAW nor conntrack, so it is what the operator has left in degraded mode.
    rx_pkts = "N/A" if metrics.net_rx_packets is None else f"{metrics.net_rx_packets:,}"
    tx_pkts = "N/A" if metrics.net_tx_packets is None else f"{metrics.net_tx_packets:,}"
    lines.append("Network volume (host tap, cumulative since the VM started)")
    lines.append(f" From VM: {bytes_to_human(metrics.net_rx_bytes):>9}  {rx_pkts:>12} pkts")
    lines.append(f" To VM:   {bytes_to_human(metrics.net_tx_bytes):>9}  {tx_pkts:>12} pkts")
    lines.append("")
    lines.append("Balance")
    lines.append(f" Current: {balance}")
    lines.append("")
    if save_dir:
        lines.append(f"\033[31m●\033[0m Recording to {save_dir}/")
        lines.append(f"    ├─ {METRICS_FILENAME}   (cpu+memory samples)")
        pcap_note = CAPTURE_FILENAME if capture_mode == "pcap" else \
            f"{CAPTURE_FILENAME} — not written (no packet capture)"
        lines.append(f"    └─ {pcap_note}")
    lines.append("─" * 72)
    source = ("AF_PACKET live capture" if capture_mode == "pcap"
              else "degraded — conntrack table, when the kernel exposes it")
    lines.append(f"Network — live flows [{source}]   (newest first, updates live)")
    if net_notice:
        lines.append(f"  ⚠ {net_notice}")
    if events:
        # Column guide for the aggregated per-flow rows.
        lines.append(f"  {'time':<8}  {'dir':<3}   "
                     f"{'peer':<40} {'proto':<5} {'packets':>10}  {'bytes':>9}")
        lines.extend(f"  {line}" for line in events[:MAX_EVENTS_DISPLAYED])
    elif not net_notice:
        lines.append("  (waiting for network activity…)")
    print("\n".join(lines), flush=True)


# --------------------------------------------------------------------------- #
# Shared live-observability core (consumed by both the CLI and Gateway.Observe).
# --------------------------------------------------------------------------- #
def observe_event_stream(
    instance_id: str,
    *,
    include_packets: bool = True,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield a running instance's live observability events, no side-effects.

    This is the single source of the AF_PACKET / metrics-sampling logic. Both
    the ``nodo observe`` CLI (which renders a TUI and writes the pcap +
    ``metrics.jsonl`` artifacts on top of these events) and the
    ``Gateway.Observe`` RPC (which serialises them to ``ObserveEvent`` protos)
    consume it — neither duplicates the capture loop.

    It resolves ``instance_id`` to a running local instance, binds an AF_PACKET
    raw socket to its tap (or falls back to the conntrack table scan), and yields
    event dicts until the instance stops, the capture socket dies, or
    ``should_stop()`` returns ``True``. It performs **no** rendering and **no**
    file writes.

    Each yielded dict carries a ``"kind"`` discriminator and a ``"time"`` field
    (``HH:MM:SS``, matching ``metrics.jsonl``):

    * ``session`` — emitted once, first. Reports how capture resolved
      (``capture_mode``, ``degraded_reason``, ``link_type``…) so a consumer can
      set up a pcap writer with the right header before data flows.
    * ``metrics`` — one CPU+memory sample per ``REFRESH_INTERVAL_S`` (exactly the
      fields of :func:`metrics_record`).
    * ``packet``  — one parsed connection event per captured frame (pcap mode) or
      conntrack row (fallback). Carries ``record`` (the flattened
      :func:`serialize_event` form, for the wire), ``raw`` (the parsed dict with
      classified ``peer``/``tag``, for per-flow aggregation) and, in pcap mode
      when ``include_packets`` is set, the verbatim ``frame`` bytes + kernel
      ``frame_ts`` so a consumer can write a pcap. When ``include_packets`` is
      ``False`` the heavy ``frame`` bytes are dropped (metrics stay the baseline
      "data"; raw packets are opt-in).
    * ``notice``  — degraded-mode / lifecycle messages (never fabricated data).

    ``instance_id`` is matched exactly against an instance id, instance name, or
    URI; there is no prefix resolution.

    :raises ObserveInstanceError: instance not found, unreachable, or not running.
    """
    instance = resolve_instance(instance_id)  # may raise ValueError / sqlite3.Error
    if instance is None:
        raise ObserveInstanceError(
            f"No local instance found matching '{instance_id}'. "
            "Use 'nodo instances' to list running instances."
        )

    full_id = instance["id"]
    father_id = instance.get("father_id") or ""
    vm_ip = instance.get("ip") or ""
    service_id = instance.get("service_id") or ""
    service_tag = _resolve_service_tag(service_id=service_id) or ""
    instance_name = instance.get("name")
    balance_display = format_balance(instance.get("balance_mu"))

    short_id = f"{full_id[:3]}..." if len(full_id) > 3 else full_id
    header = f"{instance_name} · {short_id}" if instance_name else short_id
    header += f"\nInstance ID: {full_id}"

    if service_tag:
        header += f"\nService: {service_tag} · {service_id}"

    if father_id:
        header += f"\nParent ID: {father_id}"

    # Validate it is actually running before attaching.
    try:
        first_sample = _sample_resources(full_id)
    except Exception as exc:
        raise ObserveInstanceError(
            f"Unable to attach to instance {short_id}: {exc}"
        )
    if not first_sample.get("alive"):
        raise ObserveInstanceError(
            f"Instance {short_id} is not running (no live process). "
            "Nothing to observe."
        )

    # Decide capture source (AF_PACKET on the tap, else conntrack fallback).
    tap_ifname, degraded_reason = resolve_capture_source(full_id, vm_ip)
    pcap_sock = None
    capture_mode = "conntrack"
    # Link-type + whether frames carry an ethernet header (auto-detected below).
    link_type = LINKTYPE_ETHERNET
    is_ethernet = True
    if tap_ifname:
        try:
            pcap_sock = open_packet_socket(tap_ifname)
            capture_mode = "pcap"
            link_type, is_ethernet = detect_link_type(tap_ifname)
        except OSError as exc:
            # The overwhelmingly common cause is missing CAP_NET_RAW, and `sudo` is
            # the fix — say so instead of leaving the operator with an errno.
            cause = ("needs CAP_NET_RAW — re-run as `sudo nodo observe`"
                     if isinstance(exc, PermissionError) else str(exc))
            degraded_reason = (
                f"no packet capture on '{tap_ifname}' ({cause}); "
                "no pcap will be written"
            )
            pcap_sock = None

    instance_index = build_instance_index()
    metrics = SessionMetrics()
    # Classify each peer IP once (peer/tag lookup touches disk); reuse thereafter.
    peer_cache: Dict[str, Tuple[Dict[str, Any], Optional[str]]] = {}

    def _classify(raw: Dict[str, Any]) -> Dict[str, Any]:
        peer_ip = raw.get("peer_ip", "")
        cached = peer_cache.get(peer_ip)
        if cached is None:
            peer = classify_peer(peer_ip, full_id, father_id, instance_index)
            peer_service_id = peer.get('service_id', '')
            short_peer_service_id = peer_service_id[:3] if len(peer_service_id) > 3 else peer_service_id
            peer_tag = (
                (_resolve_service_tag(peer_service_id) or "") + (" · " if short_peer_service_id else "")
                if peer.get("kind") == "instance" else ""
            ) + short_peer_service_id
            cached = (peer, peer_tag)
            peer_cache[peer_ip] = cached
        peer, peer_tag = cached
        enriched = dict(raw)
        enriched["peer"] = peer
        enriched["tag"] = peer_tag
        return enriched

    def _packet_event(raw: Dict[str, Any], frame: Optional[bytes],
                      frame_len: Optional[int], ts_float: float,
                      source: str) -> Dict[str, Any]:
        enriched = _classify(raw)
        ts_label = datetime.fromtimestamp(ts_float).strftime("%H:%M:%S")
        enriched["time"] = ts_label
        return {
            "kind": "packet",
            "time": ts_label,
            "raw": enriched,                     # parsed dict (+peer/tag) for aggregation.
            "record": serialize_event(enriched),  # flattened form for the wire.
            "frame_len": frame_len,
            "frame_ts": ts_float,
            "source": source,
            "frame": frame if include_packets else None,
        }

    def _notice(message: str, degraded: bool) -> Dict[str, Any]:
        return {
            "kind": "notice",
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": message,
            "degraded": degraded,
        }

    prev_usage = first_sample.get("cpu_usage_usec")
    prev_wall = time.monotonic_ns()
    metrics.update_memory(first_sample.get("mem_bytes"))
    metrics.update_io(first_sample)

    # Ancillary buffer big enough for one SCM_TIMESTAMPNS timespec cmsg.
    try:
        ancbufsize = socket.CMSG_SPACE(struct.calcsize(_TIMESPEC_FMT))
    except (AttributeError, OSError):
        ancbufsize = 0

    # Session event first, so a consumer can set up its pcap writer/save-dir
    # (needs capture_mode + link_type) before any data event arrives.
    yield {
        "kind": "session",
        "time": datetime.now().strftime("%H:%M:%S"),
        "instance_id": full_id,
        "father_id": father_id,
        "vm_ip": vm_ip,
        "tag": service_tag,
        "balance_mu": instance.get("balance_mu"),
        "balance_display": balance_display,
        "header": header,
        "capture_mode": capture_mode,
        "degraded_reason": degraded_reason,
        "tap_ifname": tap_ifname,
        "link_type": link_type,
        "snaplen": DEFAULT_SNAPLEN,
        "is_ethernet": is_ethernet,
    }

    try:
        next_sample = time.monotonic()
        last_conntrack_notice: Optional[str] = None
        while should_stop is None or not should_stop():
            now = time.monotonic()
            timeout = max(0.0, next_sample - now)

            # --- drain network activity --------------------------------------
            if pcap_sock is not None:
                ready, _, _ = select.select([pcap_sock], [], [], timeout)
                if ready:
                    while True:
                        try:
                            if ancbufsize:
                                data, ancdata, _, _ = pcap_sock.recvmsg(
                                    DEFAULT_SNAPLEN, ancbufsize)
                            else:
                                data, ancdata = pcap_sock.recv(DEFAULT_SNAPLEN), []
                        except BlockingIOError:
                            break
                        except OSError:
                            break
                        if not data:
                            break
                        # Kernel receive timestamp for accurate inter-packet
                        # timing, falling back to wall-clock when unavailable.
                        ts = parse_scm_timestampns(ancdata)
                        if ts is None:
                            ts = time.time()
                        parsed = parse_ethernet_frame(data, vm_ip,
                                                      is_ethernet=is_ethernet)
                        if parsed:
                            yield _packet_event(parsed, data, len(data), ts, "pcap")
                        if should_stop is not None and should_stop():
                            break
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
                yield _notice("instance stopped — ending observation", degraded=False)
                break

            cur_wall = time.monotonic_ns()
            cur_usage = sample.get("cpu_usage_usec")
            metrics.update_cpu(
                compute_cpu_percent(prev_usage, cur_usage, prev_wall, cur_wall)
            )
            prev_usage, prev_wall = cur_usage, cur_wall
            metrics.update_memory(sample.get("mem_bytes"))
            metrics.update_io(sample)

            # In conntrack fallback mode, scan the table each tick and emit its
            # flows before the metrics sample so a consumer renders both in the
            # same frame (byte counts are unavailable there → frame_len None).
            if pcap_sock is None:
                raw_events, conntrack_notice = read_conntrack_events(vm_ip)
                scan_ts = time.time()
                for raw in raw_events:
                    yield _packet_event(raw, None, None, scan_ts, "conntrack")
                # Carry *both* reasons: on its own the conntrack complaint hides the
                # fact that capture was one `sudo` away. Yielded only when it changes —
                # the scan runs every tick but the reason almost never moves.
                if conntrack_notice and conntrack_notice != last_conntrack_notice:
                    last_conntrack_notice = conntrack_notice
                    yield _notice(
                        " · ".join(filter(None, (degraded_reason, conntrack_notice))),
                        degraded=True,
                    )

            # Re-read the balance each tick so the panel reflects it being spent live.
            balance_raw = read_instance_balance(full_id)
            yield {
                "kind": "metrics",
                "balance_mu": balance_raw,
                "balance_display": format_balance(balance_raw),
                **metrics_record(metrics, alive=True),
            }
    finally:
        if pcap_sock is not None:
            try:
                pcap_sock.close()
            except OSError:
                pass


def observe(instance_id: str, save_path: Optional[str] = None) -> None:
    """Entry point for ``nodo observe <instance_id> [--save <path>]``.

    Thin front-end over :func:`observe_event_stream`: it renders the live TUI and
    (optionally) writes the pcap + ``metrics.jsonl`` artifacts, while the
    AF_PACKET / metrics capture itself lives entirely in the shared generator.
    """
    stop = {"flag": False}

    def _handle_sigint(signum, frame):  # noqa: ANN001
        stop["flag"] = True

    previous_handler = signal.signal(signal.SIGINT, _handle_sigint)

    save_dir: Optional[str] = None
    metrics_writer_cm: Optional[MetricsWriter] = None
    pcap_writer_cm: Optional[PcapWriter] = None
    metrics_writer: Optional[MetricsWriter] = None
    pcap_writer: Optional[PcapWriter] = None

    metrics = SessionMetrics()
    flow_table = FlowTable(max_rows=MAX_EVENTS_DISPLAYED)
    last_render = {"t": 0.0}
    header = instance_id
    capture_mode = "conntrack"
    base_notice: Optional[str] = None
    full_id = instance_id
    last_balance = {"v": "N/A"}

    def _do_render(notice: Optional[str]) -> None:
        flow_lines = [format_flow_line(r) for r in flow_table.active_rows()]
        _render(header, metrics, last_balance["v"], flow_lines, save_dir, notice,
                capture_mode)
        last_render["t"] = time.monotonic()

    try:
        for event in observe_event_stream(
            instance_id,
            include_packets=bool(save_path),
            should_stop=lambda: stop["flag"],
        ):
            kind = event["kind"]
            if kind == "session":
                header = event["header"]
                capture_mode = event["capture_mode"]
                base_notice = event.get("degraded_reason")
                full_id = event["instance_id"]
                last_balance["v"] = event.get("balance_display", last_balance["v"])
                if save_path:
                    save_dir = build_save_dir(save_path, event.get("tag"), full_id)
                    os.makedirs(save_dir, exist_ok=True)
                    metrics_writer_cm = MetricsWriter(
                        os.path.join(save_dir, METRICS_FILENAME))
                    metrics_writer = metrics_writer_cm.__enter__()
                    if capture_mode == "pcap":
                        pcap_writer_cm = PcapWriter(
                            os.path.join(save_dir, CAPTURE_FILENAME),
                            network=event["link_type"])
                        pcap_writer = pcap_writer_cm.__enter__()
                    else:
                        # Degraded: note why the pcap is missing (self-explanatory folder).
                        write_capture_unavailable_note(
                            save_dir,
                            base_notice or "packet capture unavailable on this host")
            elif kind == "metrics":
                metrics.cpu_current = event["cpu_percent"]
                metrics.cpu_peak = event["cpu_peak_percent"]
                metrics.mem_current = event["mem_bytes"]
                metrics.mem_peak = event["mem_peak_bytes"]
                metrics.update_io(event)  # event keys match the counter attrs.
                last_balance["v"] = event.get("balance_display", last_balance["v"])
                if metrics_writer:
                    metrics_writer.write(metrics_record(metrics, alive=event["alive"]))
                _do_render(base_notice)
            elif kind == "packet":
                raw = event["raw"]
                if pcap_writer is not None and event.get("frame") is not None:
                    pcap_writer.write_frame(event["frame"], event["frame_ts"])
                flow_table.update(
                    flow_key(raw),
                    direction=raw["direction"],
                    protocol=raw.get("protocol"),
                    peer=raw["peer"],
                    tag=raw.get("tag"),
                    frame_len=event.get("frame_len"),
                    timestamp=event["frame_ts"],
                    source=event["source"],
                )
                # Re-render on network bursts too (throttled) so a chatty flow
                # ticks up between the 1 s metrics samples.
                if (time.monotonic() - last_render["t"]) >= MIN_RENDER_INTERVAL_S:
                    _do_render(base_notice)
            elif kind == "notice":
                if event.get("message") == "instance stopped — ending observation":
                    _do_render(event["message"])
                else:
                    base_notice = event["message"]
    except ValueError as exc:
        print(str(exc), flush=True)
        return
    except sqlite3.Error as exc:
        print(f"Error reading instance catalogue: {exc}", flush=True)
        return
    except ObserveInstanceError as exc:
        print(exc.message, flush=True)
        return
    finally:
        if pcap_writer_cm:
            pcap_writer_cm.__exit__(None, None, None)
        if metrics_writer_cm:
            metrics_writer_cm.__exit__(None, None, None)
        signal.signal(signal.SIGINT, previous_handler)

    print(f"\nObservation of {short_id(full_id)} ended.", flush=True)

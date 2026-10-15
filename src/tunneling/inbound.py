"""The ``ServiceTunnel`` streams this node is relaying for others, while it relays them.

``nodo tunnels`` lists the tunnels this host *opened*; this is the other end: who is
reaching which instance through us, since when, and how many bytes have crossed so
far. It lives in memory -- a stream does not outlive the daemon -- and the daemon
mirrors it to ``<main.STORAGE>/tunnels/inbound.snapshot`` on every open and close,
and every ``REFRESH_S`` while one is open so the byte counts move. That file is what
``nodo tunnels --inbound`` and the TUI's TUNNELS page read, the way the CLI reads the
other state the daemon leaves in ``STORAGE``; reading it needs no RPC and no proto.

List-only: an operator cannot close one of these from the CLI. Nothing outside the
daemon can reach the stream, and stopping a relay cleanly would need a control path
into the daemon that does not exist; ``host_limits`` and the instance's balance are
the levers that already end them.
"""

import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from src.utils import tunnel_registry

#: How often the snapshot is rewritten while at least one stream is open.
REFRESH_S = 2.0

_lock = threading.Lock()
_streams: Dict[str, Dict[str, Any]] = {}
_writer: Optional[threading.Thread] = None
_warned = False


def opened(caller: str, token: str, slot: str, transport: str, target: str) -> str:
    """Record a stream that has just started relaying; returns its id."""
    stream_id = secrets.token_hex(4)
    with _lock:
        _streams[stream_id] = {
            "id": stream_id,
            "caller": caller,
            "token": token,
            "slot": int(slot) if str(slot).isdigit() else slot,
            "transport": transport,
            "target": target,
            "started_at": int(time.time()),
            "bytes_in": 0,
            "bytes_out": 0,
        }
    _ensure_writer()
    publish()
    return stream_id


def count(stream_id: Optional[str], bytes_in: int = 0, bytes_out: int = 0) -> None:
    """Add relayed bytes: ``in`` is caller -> service, ``out`` service -> caller."""
    if stream_id is None:
        return
    with _lock:
        stream = _streams.get(stream_id)
        if stream is not None:
            stream["bytes_in"] += bytes_in
            stream["bytes_out"] += bytes_out


def closed(stream_id: Optional[str]) -> None:
    if stream_id is None:
        return
    with _lock:
        _streams.pop(stream_id, None)
    publish()


def snapshot() -> List[Dict[str, Any]]:
    with _lock:
        return [dict(stream) for stream in _streams.values()]


def publish() -> None:
    """Write the snapshot. A node whose storage is not writable keeps relaying."""
    global _warned
    try:
        tunnel_registry.write_inbound(snapshot())
    except OSError as e:
        if not _warned:
            _warned = True
            from src.utils.logger import LOGGER

            LOGGER(f"[TUNNEL] Cannot write the inbound tunnel snapshot: {e}")


def reset() -> None:
    """Forget every stream and say so on disk: called once when the daemon starts."""
    with _lock:
        _streams.clear()
    publish()


def _refresh_loop() -> None:
    while True:
        time.sleep(REFRESH_S)
        with _lock:
            busy = bool(_streams)
        if busy:
            publish()


def _ensure_writer() -> None:
    global _writer
    with _lock:
        if _writer is not None and _writer.is_alive():
            return
        _writer = threading.Thread(target=_refresh_loop, name="tunnel-inbound-snapshot", daemon=True)
        _writer.start()

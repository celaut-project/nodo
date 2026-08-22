"""Minimal QMP (QEMU Machine Protocol) client for live guest control.

The QEMU backend boots each guest with a ``-qmp unix:<sock>`` control socket
(see :func:`src.virtualizers.qemu.execute.build_qemu_command`). This module speaks
just enough QMP to drive the memory balloon: the capabilities handshake, plus the
``balloon`` (set target) and ``query-balloon`` commands.

Why this exists: cgroup ``memory.max`` cannot shrink a running QEMU guest safely.
QEMU boots with a fixed ``-m`` allocation and, without a balloon, never returns
guest pages, so squeezing ``memory.max`` below the guest's resident set only
swaps the host process or gets it OOM-killed -- the guest is never resized and
may die (proven live on nodo#274's re-test). ``virtio-balloon`` is the correct
primitive: inflating the balloon makes the *guest* hand its free pages back to
the host (RSS drops), and deflating returns them, all cooperatively and without
OOM. This client is how :mod:`src.virtualizers.qemu.hotplug` issues that resize.
"""
import json
import socket
from typing import Any, Dict, Optional


class QMPError(RuntimeError):
    pass


class QMPClient:
    """One short-lived QMP conversation over a unix socket.

    Deliberately not persistent: hotplug is rare, so a hotplug call connects,
    handshakes, issues one or two commands and closes. Keeps no VM-side state.
    """

    def __init__(self, socket_path: str, timeout: float = 10.0):
        self._path = socket_path
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._file = None

    def __enter__(self) -> "QMPClient":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self._timeout)
        s.connect(self._path)
        self._sock = s
        self._file = s.makefile("rw")
        # Greeting banner, then the mandatory capabilities negotiation.
        self._read()  # server {"QMP": {...}} greeting
        self._execute("qmp_capabilities")

    def close(self) -> None:
        try:
            if self._file:
                self._file.close()
        finally:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self._sock = None
            self._file = None

    def _read(self) -> Dict[str, Any]:
        assert self._file is not None
        line = self._file.readline()
        if not line:
            raise QMPError("QMP connection closed unexpectedly.")
        return json.loads(line)

    def _execute(self, command: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        assert self._file is not None
        payload: Dict[str, Any] = {"execute": command}
        if arguments:
            payload["arguments"] = arguments
        self._file.write(json.dumps(payload) + "\n")
        self._file.flush()
        # Skip asynchronous events (they carry an "event" key) until the reply
        # to *this* command (a "return" or "error") arrives.
        while True:
            msg = self._read()
            if "event" in msg and "return" not in msg and "error" not in msg:
                continue
            if "error" in msg:
                raise QMPError(f"QMP command {command} failed: {msg['error']}")
            return msg.get("return", {})

    def set_balloon(self, target_bytes: int) -> None:
        """Ask the guest to size its available RAM to ``target_bytes``.

        Inflating (target < current) makes the guest return free pages to the
        host; deflating (target > current, up to boot ``-m``) hands them back.
        The guest only ever surrenders *free* pages, so this never OOMs an
        actively-using guest -- it just cannot reclaim the working set, which is
        the correct, safe semantics.
        """
        self._execute("balloon", {"value": int(target_bytes)})

    def query_balloon(self) -> Dict[str, Any]:
        return self._execute("query-balloon")

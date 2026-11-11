"""The notice: a line that the node writes to a service before it stops or hibernates it (#645).

A service that declares ``Service.Container.notice_declaration`` has a virtio console in
its VM. The node writes one line to it, ``STOP <t>`` or ``HIBERNATE <t>``, and waits until
``<t>`` or until the VM ends. The service reads the line from ``/dev/hvc0``, flushes its
state, and exits or does nothing. A service with no declaration gets no notice, and the
node stops it at once.

This module holds what the packer, the node and the virtualizers share: the one
combination that this version supports, the default declaration, the check that refuses
the others, and the lines.
"""

import errno
import http.client
import json
import os
import select
import socket
import stat
import termios
import time
from typing import Any, Dict, List, Optional

from protos import celaut_pb2
from src.virtualizers.architecture import UnsupportedArchitectureException

#: The path of the device in the container, as path segments.
SUPPORTED_PATH = ("dev", "hvc0")
#: The tags of the system that carries the notices.
SUPPORTED_SYSTEM = ("virtio-console",)
#: The tags of the format of the lines.
SUPPORTED_FORMAT = ("nodo-notice",)

#: The verbs of the lines. A service ignores a verb that it does not know.
STOP = "STOP"
HIBERNATE = "HIBERNATE"

#: The start of the name of the socket of the notice, in the directory of the control sockets.
SOCKET_PREFIX = "notice-"

#: The key of ``service.json`` that changes or turns off the declaration.
JSON_KEY = "notice_declaration"

#: A line is short. The node writes no more than this.
MAX_LINE_BYTES = 128

#: The console of the notice on Cloud Hypervisor: ``--console pty``. CH v51.1 has no
#: ``socket=`` for the console. The runtime state of the VM says ``notice_console: pty``.
CH_CONSOLE_PTY = "pty"

#: What the node reads from the pty before and after it writes, at most, and throws away
#: unread: the echo of the guest tty, and what the guest wrote while nobody was attached.
#: Never parsed. The bound keeps a guest that writes a lot from holding the node.
DISCARD_LIMIT_BYTES = 64 * 1024

#: How long a notice may take on Cloud Hypervisor: ``vm.info`` on the API socket, then the
#: write to the pty. It runs under the lock of the instance, so it is short.
CH_TIMEOUT_SECONDS = 1.0

#: ``vm.info`` is a small JSON document. The node reads no more than this of it.
_VM_INFO_LIMIT_BYTES = 1024 * 1024


class UnsupportedNoticeException(UnsupportedArchitectureException):
    """The service declares a notice that this node cannot carry.

    A node that cannot carry the notice refuses the service, as it refuses an
    architecture that it cannot run. For that reason it is an
    ``UnsupportedArchitectureException``: every place that says "this node does not run
    this service" already handles it.
    """

    def __init__(self, reason: str):
        # Not ``UnsupportedArchitectureException.__init__``: its message is about an
        # architecture.
        self.message = reason
        Exception.__init__(self, self.message)


def default_declaration() -> celaut_pb2.Service.Container.NoticeDeclaration:
    """The declaration that the packer writes when ``service.json`` does not give one."""
    declaration = celaut_pb2.Service.Container.NoticeDeclaration()
    declaration.path.extend(SUPPORTED_PATH)
    declaration.system.tags.extend(SUPPORTED_SYSTEM)
    declaration.format.tags.extend(SUPPORTED_FORMAT)
    return declaration


def _tags(value: Any, name: str) -> List[str]:
    """The tags of ``value`` (a string or a list of strings), or ``ValueError``."""
    items = [value] if isinstance(value, str) else value
    if (
        not isinstance(items, list)
        or not items
        or not all(isinstance(item, str) and item.strip() for item in items)
    ):
        raise ValueError(
            f"service.json '{JSON_KEY}.{name}' must be a non-empty string or a non-empty "
            "list of non-empty strings."
        )
    return [item.strip() for item in items]


def _path_segments(value: Any) -> List[str]:
    """The path segments of ``value`` (a string or a list of strings), or ``ValueError``."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ValueError(
            f"service.json '{JSON_KEY}.path' must be a string or a list of strings."
        )
    segments = [
        segment.strip() for item in items for segment in item.split("/") if segment.strip()
    ]
    if not segments:
        raise ValueError(f"service.json '{JSON_KEY}.path' must name a device.")
    return segments


def declaration_from_json(
    value: Any,
) -> Optional[celaut_pb2.Service.Container.NoticeDeclaration]:
    """The declaration that ``service.json`` asks for, or None for no declaration.

    No key (``value`` is None) gives the default. ``False`` gives None: the service
    declares no notice. An object gives its own ``path``, ``system`` and ``format``, and
    a field that the object does not give keeps its default. The packer checks the shape
    only. The node refuses a combination that it does not support, when it quotes or runs
    the service.
    """
    if value is None:
        return default_declaration()
    if value is False:
        return None
    if not isinstance(value, dict):
        raise ValueError(
            f"service.json '{JSON_KEY}' must be an object, or false to turn the notice off."
        )
    unknown = sorted(set(value) - {"path", "system", "format"})
    if unknown:
        raise ValueError(
            f"service.json '{JSON_KEY}' has keys that it does not know: {', '.join(unknown)}."
        )
    declaration = celaut_pb2.Service.Container.NoticeDeclaration()
    declaration.path.extend(
        _path_segments(value["path"]) if "path" in value else SUPPORTED_PATH
    )
    declaration.system.tags.extend(
        _tags(value["system"], "system") if "system" in value else SUPPORTED_SYSTEM
    )
    declaration.format.tags.extend(
        _tags(value["format"], "format") if "format" in value else SUPPORTED_FORMAT
    )
    return declaration


def is_declared(service: Optional[celaut_pb2.Service]) -> bool:
    """Whether the service declares a notice. None, or anything that is not a service,
    declares none."""
    return isinstance(service, celaut_pb2.Service) and service.container.HasField(
        "notice_declaration"
    )


def refusal(service: celaut_pb2.Service) -> Optional[str]:
    """Why this node cannot carry the notice of ``service``, or None if it can.

    None as well for a service that declares nothing. Only one combination is supported:
    the path ``/dev/hvc0``, the system ``virtio-console`` and the format ``nodo-notice``.
    """
    if not is_declared(service):
        return None
    declaration = service.container.notice_declaration
    if tuple(declaration.path) != SUPPORTED_PATH:
        return (
            f"This node cannot carry a notice to the path /{'/'.join(declaration.path)}. "
            f"It supports /{'/'.join(SUPPORTED_PATH)} only."
        )
    if tuple(declaration.system.tags) != SUPPORTED_SYSTEM:
        return (
            f"This node does not know the notice system {list(declaration.system.tags)}. "
            f"It supports {list(SUPPORTED_SYSTEM)} only."
        )
    if tuple(declaration.format.tags) != SUPPORTED_FORMAT:
        return (
            f"This node does not know the notice format {list(declaration.format.tags)}. "
            f"It supports {list(SUPPORTED_FORMAT)} only."
        )
    return None


def enforce_supported(service: Optional[celaut_pb2.Service]) -> None:
    """Raise :class:`UnsupportedNoticeException` if this node cannot carry the notice."""
    if service is None:
        return
    reason = refusal(service)
    if reason:
        raise UnsupportedNoticeException(reason)


def line(verb: str, deadline: float) -> bytes:
    """The line for ``verb`` that ends at the unix time ``deadline``, in whole seconds."""
    if verb not in (STOP, HIBERNATE):
        raise ValueError(f"unknown notice verb: {verb!r}")
    return f"{verb} {int(deadline)}\n".encode("ascii")


def socket_path(vmachine_id: str) -> str:
    """The path of the notice socket of the VM ``vmachine_id``.

    In the short, flat directory of the control sockets and not in the runtime directory
    of the VM: an ``AF_UNIX`` path holds 108 bytes at most, and a runtime directory is
    keyed by the whole id. Only the node writes to the directory, which is private.
    """
    from src.virtualizers.microvm import paths

    return str(paths.control_socket_dir() / f"{SOCKET_PREFIX}{vmachine_id[:16]}.sock")


def remove_socket(vmachine_id: str) -> None:
    """Remove the notice socket of ``vmachine_id``, if there is one.

    Before a launch, so that a VM that was killed leaves nothing that stops the next one
    from binding. In the teardown of the VM, so that nothing stays on the host.
    """
    try:
        os.unlink(socket_path(vmachine_id))
    except OSError:
        pass


def _has_socket(vmachine_id: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(socket_path(vmachine_id)).st_mode)
    except OSError:
        return False


def _runtime_state(vmachine_id: str) -> Optional[Dict[str, Any]]:
    from src.virtualizers.microvm.runtime_state import load_runtime_state

    try:
        return load_runtime_state(vmachine_id)
    except Exception:
        return None


def _ch_api_socket(vmachine_id: str) -> Optional[str]:
    """The API socket of a CH VM whose console carries the notice, else None."""
    state = _runtime_state(vmachine_id) or {}
    if state.get("notice_console") != CH_CONSOLE_PTY:
        return None
    path = state.get("control_socket")
    return path if isinstance(path, str) and path else None


def _ch_pid(vmachine_id: str) -> Optional[int]:
    """The PID of the CH process of the VM, from its runtime state, else None."""
    pid = (_runtime_state(vmachine_id) or {}).get("pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


def has_device(vmachine_id: str) -> bool:
    """Whether the VM ``vmachine_id`` has the console of the notice.

    QEMU: the socket of its ``virtconsole``. Cloud Hypervisor: a runtime state that says
    ``notice_console: pty``. A VM that started before the notice existed, or whose
    service declares none, has neither, and the node stops it at once.
    """
    return _has_socket(vmachine_id) or _ch_api_socket(vmachine_id) is not None


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def ch_console_pty(api_socket: str, *, timeout: float = CH_TIMEOUT_SECONDS) -> Optional[str]:
    """The path of the pty of the console of a CH VM, from ``GET /api/v1/vm.info``.

    None if CH does not answer, or if its console is not a pty. CH opens the pty when it
    starts, so the path changes with each process (a hibernation and its wake are two),
    and the node reads it again at each notice. Nothing is kept: a node that restarts
    finds it the same way. The path must be a pty slave (``/dev/pts/<n>``).
    """
    conn = _UnixHTTPConnection(api_socket, timeout)
    try:
        conn.request("GET", "/api/v1/vm.info")
        response = conn.getresponse()
        if response.status != 200:
            return None
        info = json.loads(response.read(_VM_INFO_LIMIT_BYTES + 1)[:_VM_INFO_LIMIT_BYTES])
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    console = ((info or {}).get("config") or {}).get("console") or {}
    if str(console.get("mode", "")).lower() != CH_CONSOLE_PTY:
        return None
    path = console.get("file")
    if not isinstance(path, str) or not _is_pty_slave_path(path):
        return None
    return path


def _is_pty_slave_path(path: str) -> bool:
    head, _, number = os.path.realpath(path).rpartition("/")
    return head == "/dev/pts" and number.isdigit()


def owns_pty(pid: int, path: str) -> bool:
    """Whether the process ``pid`` holds the master of the pty slave at ``path``.

    ``vm.info`` is the answer of the CH process, so the node does not trust the path
    that it gives: a CH that a guest took over could name the pty of another session.
    The node writes only to a pty that this process owns. A process that holds the
    master of ``/dev/pts/<n>`` has a ``/dev/ptmx`` descriptor whose ``fdinfo`` says
    ``tty-index: <n>``. False if the node cannot read the process (it ended, or the node
    has no right to look): then nothing is written.
    """
    if not _is_pty_slave_path(path):
        return False
    number = os.path.realpath(path).rpartition("/")[2]
    fd_dir = f"/proc/{int(pid)}/fd"
    try:
        descriptors = os.listdir(fd_dir)
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            if os.readlink(f"{fd_dir}/{descriptor}") != "/dev/ptmx":
                continue
            with open(f"/proc/{int(pid)}/fdinfo/{descriptor}") as info:
                for entry in info:
                    key, _, value = entry.partition(":")
                    if key == "tty-index" and value.strip() == number:
                        return True
        except OSError:
            continue
    return False


def _discard(fd: int, budget: int) -> int:
    """Read what is there, up to ``budget`` bytes, and throw it away. Never waits."""
    left = budget
    while left > 0:
        try:
            chunk = os.read(fd, min(4096, left))
        except (BlockingIOError, InterruptedError):
            break
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EIO):
                break
            raise
        if not chunk:
            break
        left -= len(chunk)
    return budget - left


def _make_raw(fd: int) -> None:
    """Raw, with no echo, no output processing, no hangup on close (#645).

    CH already makes the slave raw. The node does it again in case someone changed it,
    so the bytes reach the guest as they are written.
    """
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(
        termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
        | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON
    )
    attrs[1] &= ~termios.OPOST
    attrs[2] = (attrs[2] & ~(termios.CSIZE | termios.PARENB | termios.HUPCL)) | termios.CS8 | termios.CLOCAL
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def write_pty(
    path: str, payload: bytes, *, owner_pid: int, timeout: float = CH_TIMEOUT_SECONDS
) -> bool:
    """Write ``payload`` to the pty slave at ``path``. True if all of it was written.

    ``owner_pid`` is the process that must hold the master (the CH process of the VM).
    The node checks it before it opens the slave and again after, and writes nothing to
    a pty that another process owns (see :func:`owns_pty`).

    The node opens the slave for this write only, with ``O_NOCTTY`` (the pty never becomes
    a controlling tty of the node) and ``O_NONBLOCK`` (a CH that does not read never holds
    the node), and closes it. A guest that keeps ``/dev/hvc0`` open gets the line; a
    close on the host does not hang up the guest tty. What the guest wrote is read and
    thrown away, up to ``DISCARD_LIMIT_BYTES``, and never parsed.
    """
    if not owns_pty(owner_pid, path):
        return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        if not (stat.S_ISCHR(os.fstat(fd).st_mode) and os.isatty(fd)):
            return False
        # The pty number can pass to another session between the check and the open.
        if not owns_pty(owner_pid, path):
            return False
        _make_raw(fd)
        budget = DISCARD_LIMIT_BYTES
        budget -= _discard(fd, budget)
        end = time.monotonic() + timeout
        view = memoryview(payload)
        while view:
            try:
                view = view[os.write(fd, view):]
                continue
            except (BlockingIOError, InterruptedError):
                pass
            left = end - time.monotonic()
            if left <= 0:
                return False
            select.select([], [fd], [], left)
        _discard(fd, budget)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def deliver(vmachine_id: str, verb: str, deadline: float) -> bool:
    """Write the line to the console of the notice of the VM, whatever carries it.

    QEMU: its socket. Cloud Hypervisor: its pty, found in ``vm.info``. False if the VM
    has no such console, or the line could not be written; the caller then stops the VM
    as it does without a notice.
    """
    if _has_socket(vmachine_id):
        return send(socket_path(vmachine_id), verb, deadline)
    api_socket = _ch_api_socket(vmachine_id)
    if api_socket is None:
        return False
    pid = _ch_pid(vmachine_id)
    if pid is None:
        return False
    pty = ch_console_pty(api_socket)
    if pty is None:
        return False
    return write_pty(pty, line(verb, deadline), owner_pid=pid)


def send(path: str, verb: str, deadline: float, *, timeout: float = 2.0) -> bool:
    """Write the line to the notice socket at ``path``. True if it was written.

    The node connects, writes and closes. It never reads: a guest process can write to
    ``/dev/hvc0`` as well, so what comes back is not trusted, and the node does not
    parse it. False if nobody listens or the write fails; the caller then stops the VM
    as it does without a notice.
    """
    payload = line(verb, deadline)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(payload)
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def grace_deadline(grace_seconds: int, now: Optional[float] = None) -> float:
    """The unix time at which the node stops the VM whatever happens."""
    return (time.time() if now is None else now) + max(0, int(grace_seconds))


#: The wait of ``instances.STOP_GRACE_SECONDS`` when the config does not set it.
DEFAULT_STOP_GRACE_SECONDS = 15


def stop_grace_seconds() -> int:
    """How long the node waits after a notice, in seconds. 0 turns the notice off.

    The config validates the key at load. A bad value that is here anyway (a config object
    that skipped the load) gives the default and never raises: this runs in the manager loop.
    """
    from src.utils.config import ConfigManager

    try:
        value = ConfigManager().get(
            "instances.STOP_GRACE_SECONDS", DEFAULT_STOP_GRACE_SECONDS
        )
        if isinstance(value, bool) or not isinstance(value, int):
            return DEFAULT_STOP_GRACE_SECONDS
        return max(0, value)
    except Exception:
        return DEFAULT_STOP_GRACE_SECONDS

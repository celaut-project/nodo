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

import os
import socket
import stat
import time
from typing import Any, List, Optional

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


def has_device(vmachine_id: str) -> bool:
    """Whether the VM ``vmachine_id`` has the console of the notice.

    A VM that started before the notice existed, or whose service declares none, has no
    socket, and the node stops it at once.
    """
    try:
        return stat.S_ISSOCK(os.stat(socket_path(vmachine_id)).st_mode)
    except OSError:
        return False


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

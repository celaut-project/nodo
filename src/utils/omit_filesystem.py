"""Receive a service and not store its filesystem (``misc.OMIT_FS``).

bee-rpc writes every block of an incoming service to ``main.BLOCKDIR`` while it reads the
stream. It also needs the length of each block on disk to write the pointer form of the
service (the file that ``load_service_from_disk`` reads). It has no way to receive a
block and drop it.

This module adds that, without a change in bee-rpc. It has two parts.

``OmitSession`` filters the raw stream of buffers before bee-rpc parses it:

* It follows the markers of the blocks. A block that bee-rpc finds directly in the
  service is a "depth-1 block". Every such block is part of the filesystem: the packer
  stores the filesystem as one block, or, in the older inline shape, as one block per
  large file.
* It reads the length of each of these blocks from the varint that comes just before the
  block, in the bytes of the service. This length is the same whether the sender sends
  the body of the block or skips it.
* With ``verify``, it hashes every byte of the service, the filesystem included, so the
  caller can compare the id of the service with the one that it asked for.
* Without ``verify``, it asks the sender to skip the body (``StreamControl``). The sender
  stops, and the bytes do not cross the network.

A patch of ``save_chunks_to_block`` then reads the body of such a block off the stream,
writes nothing, and records the pointer in the ``_.json`` of the service as bee-rpc does.
A patch of the two size functions gives bee-rpc the length of the block that is not on
disk, for the time that it writes the pointer form. Both patches act only inside a
session, in the thread that entered it. Without a session, bee-rpc works as before.

The service ends as a registry entry that has its pointer form and its other blocks, and
no filesystem. The caller writes the ``.nofs`` marker (``partial_service``).
"""
import os
import threading
from contextlib import contextmanager
from typing import Dict, Generator, Iterable, List, Optional

from bee_rpc import client as bee_client
from bee_rpc import block_driver as bee_block_driver
from bee_rpc import utils as bee_utils
from bee_rpc.utils import block_id_from_pointer

from src.utils.hashing import HashSpec

# A varint has at most 10 bytes.
_TAIL_BYTES = 16

_local = threading.local()


class OmitError(Exception):
    """The stream does not have the shape that the filter needs."""


def current() -> Optional["OmitSession"]:
    return getattr(_local, "session", None)


def _trailing_varint(tail: bytes) -> Optional[int]:
    """The varint that ends ``tail``, or None if it does not end with one."""
    if not tail or tail[-1] & 0x80:
        return None
    start = len(tail) - 1
    while start > 0 and tail[start - 1] & 0x80:
        start -= 1
    value = 0
    for shift, byte in enumerate(tail[start:]):
        value |= (byte & 0x7F) << (7 * shift)
    return value


class OmitSession:
    """The state of one transfer of one service.

    ``service_index`` is the index of the partition that holds the service in the
    stream (6 in ``GetService``, 2 in a ``.celaut.bee`` file). ``spec`` is given for
    ``verify`` only.
    """

    def __init__(self, service_index: int, spec: Optional[HashSpec] = None):
        self.service_index = service_index
        self.spec = spec
        self.verify = spec is not None
        self._hasher = spec.hasher_factory() if spec is not None else None
        # What the sender sends, as the stream of this partition. Always closed
        # by ``check``.
        self._partition: Optional[int] = None
        self._stack: List[str] = []
        self._tail = bytearray()
        self._body = 0
        # Depth-1 blocks of the service: id -> length of its expansion.
        self.lengths: Dict[str, int] = {}
        # The ids in the order that they came.
        self.omitted: List[str] = []
        # The blocks that are not on disk, for the size functions of bee-rpc.
        self.virtual: Dict[str, int] = {}
        # Set by the capture of the StreamControl of the call (see ``install``).
        self.control = None

    # -- the stream

    def tap(self, buffers: Iterable) -> Generator:
        """``buffers``, unchanged, while it follows the service partition."""
        for buffer in buffers:
            if buffer.HasField("head"):
                self._partition = buffer.head.index
                self._stack = []
                self._tail = bytearray()
            if self._partition == self.service_index:
                if buffer.HasField("block"):
                    self._on_marker(buffer.block)
                elif buffer.HasField("chunk") and buffer.chunk:
                    self._on_chunk(buffer.chunk)
            yield buffer

    def _on_chunk(self, chunk: bytes) -> None:
        if self._hasher is not None:
            self._hasher.update(chunk)
        if self._stack:
            self._body += len(chunk)
        else:
            self._tail += chunk
            if len(self._tail) > _TAIL_BYTES:
                del self._tail[:-_TAIL_BYTES]

    def _on_marker(self, block) -> None:
        block_id = block_id_from_pointer(block)
        if not block_id:
            return
        if self._stack and self._stack[-1] == block_id:
            self._stack.pop()
            if not self._stack and self.verify:
                self._check_body(block_id)
            return
        self._stack.append(block_id)
        if len(self._stack) == 1:
            self._start_depth1(block_id)

    def _start_depth1(self, block_id: str) -> None:
        length = _trailing_varint(bytes(self._tail))
        if length is None:
            raise OmitError(
                f"The block {block_id} does not follow the length of its field, so its "
                f"size is not known."
            )
        self.lengths[block_id] = length
        self.omitted.append(block_id)
        self._body = 0
        if not self.verify and self.control is not None:
            self.control.request_skip(block_id)

    def _check_body(self, block_id: str) -> None:
        if self._body != self.lengths[block_id]:
            raise OmitError(
                f"The block {block_id} has {self._body} bytes, and its field says "
                f"{self.lengths[block_id]}."
            )

    def digest(self) -> bytes:
        """The hash of every byte of the service. Only with ``verify``."""
        if self._hasher is None:
            raise OmitError("This session did not hash the service.")
        return self.spec.finalize(self._hasher, self.spec.digest_size)


@contextmanager
def omit_session(session: OmitSession):
    """Make ``session`` the one of this thread. Install the patches on the first use."""
    install()
    previous = getattr(_local, "session", None)
    _local.session = session
    try:
        yield session
    finally:
        _local.session = previous


# -- the patches of bee-rpc

_installed = False
_install_lock = threading.Lock()


def _block_path_name(path: str) -> Optional[str]:
    """The block id if ``path`` is a path in the block directory, else None."""
    block_dir = os.path.normpath(bee_utils.Enviroment.block_dir)
    normal = os.path.normpath(path)
    if os.path.dirname(normal) == block_dir:
        return os.path.basename(normal)
    return None


def install() -> None:
    """Patch bee-rpc once. The patches do nothing outside a session."""
    global _installed
    with _install_lock:
        if _installed:
            return
        _installed = True

        original_save = bee_client.save_chunks_to_block
        original_getsize = bee_utils.getsize
        original_expanded = bee_utils.get_expanded_block_length
        original_control = bee_client.StreamControl

        def save_chunks_to_block(block_buffer, buffer_iterator, signal=None, _json=None,
                                 debug=lambda s: None, hashers=()):
            session = current()
            if session is not None and not hashers:
                block_id = block_id_from_pointer(block_buffer.block)
                if block_id in session.lengths:
                    if _json:
                        _json.append((block_id, list(block_buffer.block.previous_lengths_position)))
                    for buffer in buffer_iterator:
                        if buffer.HasField("block") and block_id_from_pointer(buffer.block) == block_id:
                            break
                    session.virtual[block_id] = session.lengths[block_id]
                    return None
            return original_save(
                block_buffer, buffer_iterator, signal=signal, _json=_json,
                debug=debug, hashers=hashers,
            )

        def virtual_length(path: str) -> Optional[int]:
            session = current()
            if session is None or not session.virtual:
                return None
            name = _block_path_name(path)
            if name is not None and name in session.virtual and not os.path.exists(path):
                return session.virtual[name]
            return None

        def getsize(path, _seen=None):
            length = virtual_length(path)
            return length if length is not None else original_getsize(path, _seen=_seen)

        def get_expanded_block_length(block_name, _seen=None):
            session = current()
            if session is not None and block_name in session.virtual and not os.path.exists(
                    os.path.join(bee_utils.Enviroment.block_dir, block_name)):
                return session.virtual[block_name]
            return original_expanded(block_name=block_name, _seen=_seen)

        class CapturingControl(original_control):
            """The control that ``client_grpc`` makes, kept for the session of the thread."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                session = current()
                if session is not None:
                    session.control = self

        bee_client.save_chunks_to_block = save_chunks_to_block
        bee_client.StreamControl = CapturingControl
        for module in (bee_utils, bee_block_driver):
            module.getsize = getsize
        bee_utils.get_expanded_block_length = get_expanded_block_length

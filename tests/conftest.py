"""
Test bootstrap helpers.

The generated protobuf stubs import ``buffer_pb2`` from the ``bee_rpc`` package
(``from bee_rpc import buffer_pb2``). ``bee_rpc`` is the node's runtime RPC
framework and is not always installed in a bare unit-test environment. When it
is missing we register a minimal shim that exposes only ``bee_rpc.buffer_pb2``
(backed by the local ``protos/buffer_pb2.py``) so that pure-logic tests which
merely need the ``celaut_pb2`` message classes can import them.

The shim deliberately provides *nothing else* from ``bee_rpc`` (no ``client``,
no ``parse_from_buffer``): tests that exercise the full RPC stack still fail to
import and skip themselves, exactly as before. When the real ``bee_rpc`` is
installed this shim is not registered at all.
"""
import importlib
import sys
import types


def _install_bee_rpc_buffer_shim() -> None:
    try:
        import bee_rpc  # noqa: F401
        return
    except Exception:
        pass

    try:
        buffer_pb2 = importlib.import_module("protos.buffer_pb2")
    except Exception:
        return

    shim = types.ModuleType("bee_rpc")
    shim.buffer_pb2 = buffer_pb2
    shim.__shim__ = True
    sys.modules["bee_rpc"] = shim
    sys.modules["bee_rpc.buffer_pb2"] = buffer_pb2


_install_bee_rpc_buffer_shim()

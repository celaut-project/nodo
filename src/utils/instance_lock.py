"""One lock per local instance, held by whatever changes whether its VM runs.

`suspend_instance`, `revive_instance` and `stop_instance` each take it. A revive boots
the guest, which takes seconds, and a stop that ran in the middle of it used to mark the
row dead while the new VM went on running on a deleted disk, unbilled, with its address
free for the next launch (F2 of the review of #595). Under the lock the stop waits for
the boot and then stops the VM that came up.

The callers are not all in one process: the daemon's manager thread and its gRPC
threads stop and revive, and `nodo kill` stops from the CLI. So the lock is an
``flock`` on ``STORAGE/instance_locks/<id>.lock``, which both see, and inside one
process an ``RLock`` per instance, which makes it reentrant for the thread that holds
it. A process that cannot open the file (a CLI run without the rights the daemon has)
still serialises its own threads, and says so.
"""

import fcntl
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

from src.utils import logger as log
from src.utils.config import ConfigManager

env_manager = ConfigManager()

DIR_NAME = "instance_locks"

_locks: Dict[str, threading.RLock] = {}
_guard = threading.Lock()
# The open lock file of each instance whose lock this process holds, and how deep.
_held: Dict[str, list] = {}


def lock_dir() -> Path:
    storage = env_manager.get("STORAGE")
    if not storage:
        raise RuntimeError("STORAGE path is not configured.")
    return Path(str(storage)) / DIR_NAME


def lock_path(instance_id: str) -> Path:
    return lock_dir() / f"{instance_id}.lock"


def _thread_lock(instance_id: str) -> threading.RLock:
    with _guard:
        return _locks.setdefault(instance_id, threading.RLock())


@contextmanager
def instance_lock(instance_id: str) -> Iterator[None]:
    """Hold the lock of ``instance_id`` for the ``with`` block. Reentrant per thread."""
    rlock = _thread_lock(instance_id)
    with rlock:
        held = _held.get(instance_id)
        if held is not None:
            held[1] += 1
            try:
                yield
            finally:
                held[1] -= 1
            return
        handle = None
        try:
            path = lock_path(instance_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a")
            fcntl.flock(handle, fcntl.LOCK_EX)
        except Exception as e:
            if handle is not None:
                handle.close()
            handle = None
            log.LOGGER(f"Cannot take the lock file of instance {instance_id} ({e}); "
                       f"only this process is serialised.")
        _held[instance_id] = [handle, 1]
        try:
            yield
        finally:
            del _held[instance_id]
            if handle is not None:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                finally:
                    handle.close()


def forget(instance_id: str) -> None:
    """Remove the lock file of an instance that is dead. Called under its lock.

    A process waiting on the old file gets the lock after this one and finds the
    instance dead, which is all that a dead instance needs: every caller checks the
    row under the lock.
    """
    try:
        os.unlink(lock_path(instance_id))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.LOGGER(f"Could not remove the lock file of instance {instance_id}: {e}")
    with _guard:
        _locks.pop(instance_id, None)

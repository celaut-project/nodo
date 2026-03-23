import os
from typing import Callable

from src.utils import logger as log
from src.virtualizers.cloud_hypervisor.runtime_state import load_runtime_state


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # If we cannot signal it, the process still exists.
        return True
    except Exception:
        return False


def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    state = load_runtime_state(vmachine_id)
    if not state:
        if debug_mode:
            log.LOGGER(f"[CH][{vmachine_id}] maintain: runtime state missing.")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    pid = int(state.get("pid") or 0)
    if pid <= 0:
        if debug_mode:
            log.LOGGER(f"[CH][{vmachine_id}] maintain: invalid PID in runtime state ({pid}).")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if not _pid_alive(pid):
        if debug_mode:
            log.LOGGER(f"[CH][{vmachine_id}] maintain: process not alive (pid={pid}).")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    api_socket = str(state.get("api_socket") or "").strip()
    if api_socket and not os.path.exists(api_socket):
        if debug_mode:
            log.LOGGER(
                f"[CH][{vmachine_id}] maintain: api socket missing for alive process "
                f"(pid={pid}, socket={api_socket})."
            )
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if debug_mode:
        log.LOGGER(
            f"[CH][{vmachine_id}] maintain ok: pid={pid}, api_socket={api_socket or '<none>'}"
        )

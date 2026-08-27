import os
from typing import Callable

from src.database.sql_connection import SQLConnection
from src.utils import logger as log
from src.virtualizers.ch.kill import kill as kill_ch_vm
from src.virtualizers.ch.process import pid_alive
from src.virtualizers.ch.runtime_state import load_runtime_state
from src.virtualizers.ch.runtime_state import list_runtime_states

sc = SQLConnection()


def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    state = load_runtime_state(vmachine_id)
    if not state:
        log.LOGGER(f"[CH][{vmachine_id}] event=maintain unhealthy reason=runtime_state_missing")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    pid = int(state.get("pid") or 0)
    if pid <= 0:
        log.LOGGER(f"[CH][{vmachine_id}] event=maintain unhealthy reason=invalid_pid pid={pid}")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if not pid_alive(pid, vmachine_id=vmachine_id):
        log.LOGGER(f"[CH][{vmachine_id}] event=maintain unhealthy reason=process_dead pid={pid}")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    api_socket = str(state.get("api_socket") or "").strip()
    if api_socket and not os.path.exists(api_socket):
        log.LOGGER(
            f"[CH][{vmachine_id}] event=maintain unhealthy "
            f"reason=api_socket_missing pid={pid} socket={api_socket}"
        )
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if debug_mode:
        log.LOGGER(
            f"[CH][{vmachine_id}] event=maintain healthy pid={pid}, api_socket={api_socket or '<none>'}"
        )


def janitor_cleanup_orphans(debug_mode: bool = False) -> None:
    states = list_runtime_states()
    if not states:
        return

    for vmachine_id, state in states.items():
        pid = int((state or {}).get("pid") or 0)
        in_db = sc.internal_instance_exists(id=vmachine_id)
        alive = pid_alive(pid, vmachine_id=vmachine_id) if pid > 0 else False
        # `execute` writes the state file the instant the hypervisor process exists
        # and registers the instance immediately after (see `save_booting_state`),
        # so for the width of those two writes a live VM legitimately has no row
        # yet. Killing it there would race the launcher for the VM it is still
        # setting up. Nothing leaks: a boot that fails deletes its own state, and a
        # process that dies is still caught below, `booting` or not.
        booting = bool((state or {}).get("booting")) and alive

        reason = None
        if not in_db and not booting:
            reason = "orphan_runtime_state"
        elif not alive:
            reason = "stale_runtime_process_dead"

        if not reason:
            continue

        log.LOGGER(
            f"[CH][{vmachine_id}] event=janitor cleanup_start "
            f"reason={reason} in_db={in_db} pid={pid} alive={alive}"
        )
        try:
            kill_ch_vm(vmachine_id=vmachine_id)
            log.LOGGER(
                f"[CH][{vmachine_id}] event=janitor cleanup_done "
                f"reason={reason}"
            )
        except Exception as e:
            log.LOGGER(
                f"[CH][{vmachine_id}] event=janitor cleanup_failed "
                f"reason={reason} error={e}"
            )
            if debug_mode:
                raise

"""Health check for a running QEMU guest.

Mirrors :func:`src.virtualizers.ch.maintain.maintain`: an instance whose runtime
state is gone, whose PID is invalid, or whose process is dead (matched by the
QEMU visible name) is removed and penalized.

A live process is not a live guest, so a panicked kernel is reaped here too --
QEMU outlives its guest, and under TCG the panic spin costs a host core for as
long as nobody looks.
"""
from typing import Callable

from src.utils import logger as log
from src.virtualizers.ch.guest_panic import guest_panic_line
from src.virtualizers.ch.runtime_state import load_runtime_state
from src.virtualizers.qemu.process import pid_alive


def maintain(vmachine_id: str, debug_mode: bool, remove_and_penalize: Callable[[str], None]) -> None:
    state = load_runtime_state(vmachine_id)
    if not state:
        log.LOGGER(f"[QEMU][{vmachine_id}] event=maintain unhealthy reason=runtime_state_missing")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    pid = int(state.get("pid") or 0)
    if pid <= 0:
        log.LOGGER(f"[QEMU][{vmachine_id}] event=maintain unhealthy reason=invalid_pid pid={pid}")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if not pid_alive(pid, vmachine_id=vmachine_id):
        log.LOGGER(f"[QEMU][{vmachine_id}] event=maintain unhealthy reason=process_dead pid={pid}")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    panic = guest_panic_line(state)
    if panic:
        log.LOGGER(
            f"[QEMU][{vmachine_id}] event=maintain unhealthy reason=guest_panicked "
            f"pid={pid} panic={panic!r}"
        )
        # Kill the emulator here rather than leaving it to the teardown below.
        # `stop_instance` only kills what it finds registered, and the whole
        # point of this branch is a process that no liveness test will report
        # again: if the row is already gone, the core stays pinned forever.
        # Killing a process that teardown then kills again is harmless.
        from src.virtualizers.qemu.kill import kill as qemu_kill
        try:
            qemu_kill(vmachine_id=vmachine_id)
        except Exception as e:
            log.LOGGER(f"[QEMU][{vmachine_id}] event=maintain panic_kill_failed error={e}")
        remove_and_penalize(vmachine_id=vmachine_id)
        return

    if debug_mode:
        log.LOGGER(f"[QEMU][{vmachine_id}] event=maintain healthy pid={pid}")

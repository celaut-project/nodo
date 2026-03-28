import shutil
from pathlib import Path

from src.utils import logger as log
from src.utils.config import ConfigManager
from src.virtualizers.ch.kill import kill
from src.virtualizers.ch.runtime_state import load_runtime_state

env_manager = ConfigManager()
CACHE = env_manager.get("CACHE")


def _runtime_vm_dir(vmachine_id: str) -> Path:
    return Path(CACHE) / "cloud_hypervisor" / "runtime" / vmachine_id


def _service_bundle_dir(service_id: str) -> Path:
    return Path(CACHE) / "cloud_hypervisor" / service_id


def remove(vmachine_id: str) -> bool:
    if not CACHE:
        log.LOGGER(f"[CH][{vmachine_id}] remove failed: CACHE path is not configured.")
        return False

    # Dual mode: runtime VM cleanup if state/runtime exists; otherwise remove service bundle.
    if load_runtime_state(vmachine_id) is not None or _runtime_vm_dir(vmachine_id).exists():
        log.LOGGER(f"[CH][{vmachine_id}] event=remove mode=runtime")
        return kill(vmachine_id=vmachine_id)

    bundle_dir = _service_bundle_dir(vmachine_id)
    if not bundle_dir.exists():
        log.LOGGER(
            f"[CH][{vmachine_id}] remove requested but no runtime state nor service bundle directory found."
        )
        return True

    try:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        log.LOGGER(f"[CH][{vmachine_id}] event=remove mode=bundle bundle_removed={bundle_dir}")
        return True
    except Exception as e:
        log.LOGGER(f"[CH][{vmachine_id}] remove bundle failed ({bundle_dir}): {e}")
        return False

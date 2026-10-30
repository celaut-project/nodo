"""What the node keeps on disk about instances that are dead.

When an instance dies, its row stays in the database with `killed_at` set. This
module keeps the files that go with the row, under ``STORAGE/instances_dead/<id>/``:

::

    stdout.log, stderr.log, serial.log   the last bytes of each guest log
    runtime_state.json                   the runtime state of the guest at death
    death.json                           when and why the instance died

The directory belongs to the operator. The retention sweep removes it when it
removes the row. Nothing in this module starts, stops or changes an instance.
"""
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.utils import logger as log
from src.utils.config import ConfigManager

env_manager = ConfigManager()

DIR_NAME = "instances_dead"
LOG_NAMES = ("stdout", "stderr", "serial")
# What a suspension leaves of the run that just ended (issue #594): each log is moved to
# ``<log>.1`` before the instance is revived, so the boot that follows does not write
# over the panic or the exit that caused the revive. Archived as ``<name>.previous.log``.
PREVIOUS_SUFFIX = ".1"
PREVIOUS = "previous"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_ROWS = 200
DEFAULT_LOG_MAX_BYTES = 1024 * 1024
DEFAULT_SUSPENDED_GRACE_SECONDS = 7 * 24 * 3600
DEFAULT_REVIVE_FAILURE_GRACE_SECONDS = 24 * 3600

# Reasons recorded in `death_reason`. One name for one cause.
MANUAL_KILL = "manual_kill"
OUT_OF_BALANCE = "out_of_balance"
PROCESS_LOST = "process_lost"
GUEST_PANIC = "guest_panic"
ACTIVITY_WINDOW = "activity_window"
PEER_STOPPED = "peer_stopped"
LAUNCH_FAILED = "launch_failed"
BURNALL = "burnall"

# An instance id is a hash, so it holds hex digits only. Other text is refused to
# make sure an id can never name a path outside the dead-instance directory.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def retention_days() -> int:
    return int(env_manager.get("instances.DEAD_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))


def retention_max_rows() -> int:
    return int(env_manager.get("instances.DEAD_MAX_ROWS", DEFAULT_MAX_ROWS))


def log_max_bytes() -> int:
    return int(env_manager.get("instances.DEAD_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES))


def suspended_grace_seconds() -> int:
    """How long an instance that ran out of MU keeps its disk before it is deleted.

    ``0`` (or less) means no grace: it dies at once and its disk goes, as it did
    before suspension existed.
    """
    try:
        return max(0, int(env_manager.get(
            "instances.SUSPENDED_GRACE_SECONDS", DEFAULT_SUSPENDED_GRACE_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_SUSPENDED_GRACE_SECONDS


def revive_failure_grace_seconds() -> int:
    """How long a suspended instance with MU may keep failing to start before it is stopped.

    Counted from the first revive of a run that failed (the service is gone from the
    registry, the host has no room for it). The instance is then stopped with a refund,
    and dies with the cause it was suspended for. ``0`` stops it at the first failure.
    """
    try:
        return max(0, int(env_manager.get(
            "instances.REVIVE_FAILURE_GRACE_SECONDS", DEFAULT_REVIVE_FAILURE_GRACE_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_REVIVE_FAILURE_GRACE_SECONDS


def dead_root() -> Path:
    storage = env_manager.get("STORAGE")
    if not storage:
        raise RuntimeError("STORAGE path is not configured.")
    return Path(str(storage)) / DIR_NAME


def dead_dir(instance_id: str) -> Path:
    if not _SAFE_ID.match(instance_id or "") or instance_id in (".", ".."):
        raise ValueError(f"Invalid instance id: {instance_id!r}")
    return dead_root() / instance_id


def _tail_copy(source: Path, target: Path, max_bytes: int) -> bool:
    """Copy the last ``max_bytes`` of ``source`` to ``target``. Return True on success."""
    try:
        size = source.stat().st_size
        with open(source, "rb") as reader:
            if max_bytes > 0 and size > max_bytes:
                reader.seek(size - max_bytes)
            data = reader.read()
        target.write_bytes(data)
        target.chmod(0o600)
        return True
    except OSError as e:
        log.LOGGER(f"Could not archive log {source}: {e}")
        return False


def archive_runtime(instance_id: str, state: Dict[str, Any]) -> None:
    """Save the guest logs and the runtime state of an instance that is about to die.

    Call it after the guest process stopped and before the runtime directory is
    removed. Never raises: a failed archive must not stop the teardown.
    """
    if not state:
        # The state is gone: an earlier teardown already archived this instance.
        return
    try:
        target = dead_dir(instance_id)
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
        limit = log_max_bytes()
        for name in LOG_NAMES:
            source = state.get(f"{name}_log")
            if isinstance(source, str) and source.strip():
                _tail_copy(Path(source), target / f"{name}.log", limit)
                previous = Path(source + PREVIOUS_SUFFIX)
                if previous.is_file():
                    _tail_copy(previous, target / f"{name}.{PREVIOUS}.log", limit)
        state_file = target / "runtime_state.json"
        state_file.write_text(json.dumps(state, indent=2, default=str))
        state_file.chmod(0o600)
    except Exception as e:
        log.LOGGER(f"Could not archive the runtime of {instance_id}: {e}")


def write_death_record(instance_id: str, record: Dict[str, Any]) -> None:
    """Write ``death.json`` for an instance. Never raises."""
    try:
        target = dead_dir(instance_id)
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
        death_file = target / "death.json"
        death_file.write_text(json.dumps(record, indent=2, default=str))
        death_file.chmod(0o600)
    except Exception as e:
        log.LOGGER(f"Could not write the death record of {instance_id}: {e}")


def read_death_record(instance_id: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads((dead_dir(instance_id) / "death.json").read_text())
    except (OSError, ValueError):
        return None


def list_logs(instance_id: str) -> Dict[str, Path]:
    """Return the archived log files of an instance, by name."""
    try:
        base = dead_dir(instance_id)
    except ValueError:
        return {}
    names = [name for log_name in LOG_NAMES for name in (log_name, f"{log_name}.{PREVIOUS}")]
    return {
        name: base / f"{name}.log"
        for name in names
        if (base / f"{name}.log").is_file()
    }


def keep_previous_logs(state: Dict[str, Any]) -> None:
    """Move each log of a run that ended to ``<log>.1``, over the one before.

    For a VM that stops while its instance stays (a suspension): the next boot opens its
    logs afresh, and the run that failed is the one the operator needs to read. Only the
    last failed run is kept beside the disk, so a crash loop does not fill it. Never
    raises.
    """
    for name in LOG_NAMES:
        source = state.get(f"{name}_log")
        if not (isinstance(source, str) and source.strip()):
            continue
        try:
            if Path(source).is_file():
                Path(source).replace(source + PREVIOUS_SUFFIX)
        except OSError as e:
            log.LOGGER(f"Could not keep the log {source} of the run that ended: {e}")


def logs_left_in(runtime_dir: Path) -> Dict[str, Any]:
    """The log paths of a runtime directory whose state file is gone, as a state has them.

    A suspension deletes the state file and keeps the directory, and the logs in it are
    named ``<backend>.<name>.log``. A stop that finds no state archives these.
    """
    found: Dict[str, Any] = {}
    try:
        for name in LOG_NAMES:
            matches = sorted(runtime_dir.glob(f"*.{name}.log"))
            if matches:
                found[f"{name}_log"] = str(matches[0])
    except OSError:
        pass
    return found


def remove_dirs(instance_ids: Iterable[str]) -> None:
    """Remove the archive directories of the given instances."""
    for instance_id in instance_ids:
        try:
            shutil.rmtree(dead_dir(instance_id), ignore_errors=True)
        except ValueError:
            continue


def sweep_orphan_dirs(known_ids: Iterable[str]) -> List[str]:
    """Remove the archive directories that have no row. Return the ids removed."""
    known = set(known_ids)
    removed: List[str] = []
    try:
        root = dead_root()
        entries = list(root.iterdir()) if root.is_dir() else []
    except Exception:
        return removed
    for entry in entries:
        if entry.is_dir() and entry.name not in known:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed

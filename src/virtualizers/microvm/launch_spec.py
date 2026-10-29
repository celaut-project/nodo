"""What a revive needs to know about how an instance was launched (issue #594).

An instance that is started again from its own disk has, on that disk, everything the
guest reads: the configuration, the entrypoint, the environment. What it does not have
is what the *host* decided at launch and wrote nowhere else: which host ports were
assigned to its slots, whether it is published on the host at all, and which peers its
firewall let it reach.

That is kept in a file in the instance's runtime directory, next to the rootfs, and
not in the database, for two reasons. It lives and dies with the disk it describes: a
kill, a burn or the end of a grace period removes the directory and the spec with it,
and nothing has to remember to clean a row. And it holds nothing secret -- the launcher
keeps secrets out of the database on purpose (``_serialize_envs``), and the configuration
that carries them stays where it already was, on the disk.
"""
import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from protos import celaut_pb2 as celaut

SPEC_NAME = "launch_spec.json"


@dataclass
class LaunchSpec:
    assigment_ports: Dict[int, int] = field(default_factory=dict)
    by_local: bool = True
    network_resolution: List[celaut.ConfigurationFile.NetworkResolution] = field(default_factory=list)
    # False for an instance whose launch has state outside its own directory that a
    # revive does not rebuild (a shared filesystem: its daemons and its mounts belong
    # to the launch). Such an instance dies as it always did.
    revivable: bool = True


def spec_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / SPEC_NAME


def write(runtime_dir: Path, spec: LaunchSpec) -> None:
    """Write the spec, readable by this user only. Never raises: a launch is not
    failed for a revive that may never be needed -- without the file the instance is
    simply not revivable."""
    from src.utils import logger as log

    try:
        envelope = celaut.ConfigurationFile(network_resolution=spec.network_resolution)
        payload = {
            "assigment_ports": {str(internal): int(external)
                                for internal, external in spec.assigment_ports.items()},
            "by_local": bool(spec.by_local),
            "network_resolution": base64.b64encode(envelope.SerializeToString()).decode("ascii"),
            "revivable": bool(spec.revivable),
        }
        path = spec_path(runtime_dir)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        log.LOGGER(f"Could not write the launch spec in {runtime_dir}: {e}")


def read(runtime_dir: Path) -> Optional[LaunchSpec]:
    """The spec of the instance whose directory this is, or None if there is none or it
    cannot be read -- which means the instance cannot be revived."""
    try:
        with open(spec_path(runtime_dir), "r", encoding="utf-8") as f:
            payload = json.load(f)
        envelope = celaut.ConfigurationFile()
        envelope.ParseFromString(base64.b64decode(payload.get("network_resolution") or ""))
        return LaunchSpec(
            assigment_ports={int(internal): int(external)
                             for internal, external in (payload.get("assigment_ports") or {}).items()},
            by_local=bool(payload.get("by_local", True)),
            network_resolution=list(envelope.network_resolution),
            revivable=bool(payload.get("revivable", False)),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None
    except Exception:
        return None

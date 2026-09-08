"""Authorizing a shared filesystem: does this instance's parent actually export it?

``guest=true`` is an execution precondition (see
``src/utils/shared_filesystems.py``): a service that declares an inherited
directory cannot run unless an instance exists that created that directory and
exported it to *this* one. So the question has to be answered **before anything
is spent** -- before the balancer is consulted, before MU is charged, before a
rootfs is built and a virtiofsd is started -- and its answer is the launch's, not
the backend's.

That is also why this is not a *local* preflight: if the share does not attach,
the service is not runnable on **any** peer, so there is no other candidate to
try. It fails the launch outright.

Deciding it needs what only the node's records hold -- which service the parent
is running, and the environment it was launched with -- so this is the layer
where the pure model in ``src.utils.shared_filesystems`` meets the database, the
same way ``src.manager.networks`` does for ``Service.Network``. Unlike Networks
there is no ancestor walk: a parent exports a directory out of its *own* image
and needs no ancestor's permission to do so, so the grant is not inductive and
stops at the direct parent (which is also the whole authorization -- no
re-export, no transfer).
"""
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils.registry_errors import ServiceNotInRegistry, ServiceSpecUnavailable
from src.utils.shared_filesystems import (
    ShareRef,
    SharedDir,
    exported_refs,
    guest_refs,
    share_ref,
)
from src.utils.utils import load_service_from_disk

sc = SQLConnection()

# `nodo ggconf` records its sandbox as a local instance whose service_id is not a
# registry hash but a path (`rundev::<path>`), so its exports cannot be read off a
# spec. See `rundev_exports`.
RUNDEV_PREFIX = "rundev"


class ShareAuthorizationError(Exception):
    """A declared ``guest`` directory is not granted, so the service cannot run.

    Raised instead of starting the instance anyway: from inside a guest, a share
    that came up empty is indistinguishable from one it was never given, and a
    job that returns an empty result is the failure this exception exists to
    replace.
    """


def instance_env_values(instance_id: str) -> Dict[str, bytes]:
    """The environment an instance was **launched** with, from the node's records.

    A share's discriminator has to be read from the launch environment rather
    than from anything current: the exporter resolved its own share ids at boot,
    from those values, and they are what a child has to match against.
    """
    raw = sc.get_local_instance_envs(id=instance_id)
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return {
        key: value.encode("utf-8") if isinstance(value, str) else bytes(value)
        for key, value in (stored or {}).items()
    }


def parent_exported_shares(father_id: str) -> List[ShareRef]:
    """The shares the instance that launched this one exports, from its own spec.

    Raises :class:`ShareAuthorizationError` when they cannot be established --
    which is a refusal, not an empty grant. A spec that failed to load is not a
    spec that exported nothing.
    """
    if not father_id:
        raise ShareAuthorizationError(
            "this service inherits a shared filesystem, but it was launched with "
            "no parent instance at all, so there is nothing that could have "
            "exported one."
        )

    if RUNDEV_PREFIX in father_id:
        return [ref for ref, _host_dir in _rundev_declared(father_id)]

    if not sc.internal_instance_exists(id=father_id):
        # A client (`nodo execute` draws a dev client id) has no filesystem to
        # export, and neither has a parent running on another node: the export is
        # materialized on the host from the parent's own rootfs, so a parent that
        # is not a local instance cannot have one here. Deny-by-default is not a
        # policy choice here, it is the only coherent reading -- and it is what
        # closes the recycled dev-client-id leak, since two unrelated `execute`
        # calls drawing the same id can no longer reach one another's directory.
        raise ShareAuthorizationError(
            f"this service inherits a shared filesystem from its parent, but "
            f"'{father_id}' is not an instance running on this node -- a client, or "
            f"a parent on another node, has no filesystem to export. A service with "
            f"a 'guest' directory can only be launched by the service that exports it."
        )

    service_id = sc.get_service_id_by_container_id(id=father_id)
    try:
        spec = load_service_from_disk(service_hash=service_id)
    except ServiceSpecUnavailable as e:
        raise ShareAuthorizationError(
            f"cannot establish what {father_id} exports: the spec of its service "
            f"{service_id} is on the registry but was not loadable ({e}). Nothing "
            f"is granted; retry the launch when the node is less loaded."
        ) from e
    except ServiceNotInRegistry as e:
        raise ShareAuthorizationError(
            f"cannot establish what {father_id} exports: its service {service_id} is "
            f"not on the local registry ({e}). Every spec this node launches is "
            f"stored before it runs, so a missing one is an inconsistent registry."
        ) from e

    return exported_refs(spec, father_id, instance_env_values(father_id))


def _rundev_declared(father_id: str) -> List[Tuple[ShareRef, Optional[str]]]:
    """Parse a rundev sandbox's ``__shares__`` file into (share, host directory).

    A rundev parent has no image and no spec, so a service with a ``guest``
    directory would be undevelopable under it. The mechanism is explicit rather
    than accidental: the sandbox declares what it exports in a ``__shares__``
    JSON file beside its ``__config__``, each entry naming a host directory to
    hand over::

        [{"tag": "hdfs-data", "dir": "/home/me/hdfs", "env": "HDFS_CLUSTER",
          "value": "prod", "path": "/data", "access": "rw"}]

    ``dir`` is the developer's own directory: the node mounts it as the share,
    never seeds it and never deletes it.
    """
    # container_id is "rundev::<path>::<pid>"; the sandbox's directory is the
    # middle field.
    parts = (father_id or "").split("::")
    if len(parts) < 2:
        raise ShareAuthorizationError(
            f"cannot read the rundev sandbox directory out of '{father_id}'."
        )
    shares_file = Path(parts[1]) / "__shares__"
    if not shares_file.is_file():
        raise ShareAuthorizationError(
            f"this service inherits a shared filesystem, and its rundev parent "
            f"declares none: write the exports to '{shares_file}' (a JSON list of "
            f"{{tag, dir, env, value, path, access}}) to develop it under `nodo ggconf`."
        )
    try:
        entries = json.loads(shares_file.read_text(encoding="utf-8")) or []
    except (OSError, ValueError) as e:
        raise ShareAuthorizationError(f"cannot read '{shares_file}': {e}") from e

    declared: List[Tuple[ShareRef, Optional[str]]] = []
    for entry in entries:
        tag = str(entry.get("tag") or "")
        env = str(entry.get("env") or "")
        value = entry.get("value")
        declaration = SharedDir(
            path=str(entry.get("path") or tag),
            shared=True,
            guest=False,
            access=str(entry.get("access") or "rw"),
            tag=tag,
            env=env,
        )
        env_values = (
            {env: str(value).encode("utf-8")} if env and value is not None else {}
        )
        host_dir = entry.get("dir")
        declared.append(
            (share_ref(father_id, declaration, env_values),
             str(host_dir) if host_dir else None)
        )
    return declared


def rundev_host_dirs(father_id: str) -> Dict[str, str]:
    """``share_id`` -> host directory for a rundev parent's declared exports.

    Empty for any other kind of parent, whose shares the node materializes and
    owns itself. A malformed declaration yields nothing rather than raising: by
    the time a backend asks this, authorization has already passed on the same
    file.
    """
    if RUNDEV_PREFIX not in (father_id or ""):
        return {}
    try:
        declared = _rundev_declared(father_id)
    except ShareAuthorizationError:
        return {}
    return {ref.share_id: host_dir for ref, host_dir in declared if host_dir}


def _explain(wanted: ShareRef, exports: List[ShareRef]) -> str:
    """Why this declaration is not granted, in terms the launcher can act on.

    Non-membership of a set of hashes has two opposite causes, and saying which
    one it is is the difference between "you launched this from the wrong parent"
    and "you have a typo in an environment variable".
    """
    same_name = [e for e in exports if e.name == wanted.name]
    if not same_name:
        offered = ", ".join(sorted(e.describe() for e in exports)) or "nothing"
        return (
            f"its parent does not export {wanted.describe()} at all (it exports "
            f"{offered}). This is a composition error: the service was launched by "
            f"a parent that does not provide the share it asks for."
        )
    theirs = same_name[0]
    if theirs.env != wanted.env:
        return (
            f"its parent exports '{wanted.name}' discriminated by "
            f"{theirs.env or '<no variable>'}, while this service asks for it "
            f"discriminated by {wanted.env or '<no variable>'}. Both sides must "
            f"name the same share_env for the share to be the same one."
        )
    return (
        f"its parent exports '{wanted.name}' with {wanted.env}="
        f"{theirs.discriminator or '<unset>'}, while this service was launched with "
        f"{wanted.env}={wanted.discriminator or '<unset>'}. This is a configuration "
        f"error: the two must match for it to be the same share."
    )


def authorize_shares(
    service: celaut.Service,
    father_id: str,
    config: Optional[celaut.Configuration] = None,
) -> List[ShareRef]:
    """The shares this service is granted, or raise :class:`ShareAuthorizationError`.

    A service that declares no ``guest`` directory returns an empty list without
    consulting anything -- the ordinary case pays nothing for this check.
    """
    env_values = dict(config.environment_variables) if config else {}
    wanted = guest_refs(service, father_id or "__no_father__", env_values)
    if not wanted:
        return []

    exports = parent_exported_shares(father_id)
    granted = {e.share_id for e in exports}
    for ref in wanted:
        if ref.share_id not in granted:
            raise ShareAuthorizationError(
                f"cannot inherit '{ref.path}': {_explain(ref, exports)}"
            )
    return wanted

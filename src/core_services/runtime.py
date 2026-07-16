"""Auto-execute a core service: resolve (and, if needed, launch) it by id and
return a live HTTP endpoint.

This is the runtime symmetry to :mod:`src.core_services.source_application`. Where
``source_application.acquire_service`` *downloads* a missing core service into the
local registry, this module makes sure such a service is actually *running* and hands
the caller a reachable ``http://<ip>:<port>`` endpoint it can talk to. A future
``nodo pack`` packer path, for example, can call
:func:`ensure_core_service_running` with the configured ``packer`` core service id and
get back the live endpoint of a packer instance (downloading + launching it on demand).

Fail-closed / best-effort contract (mirrors source-application's tone):
    * Nothing here ever raises into the caller. Every step — reading the local
      instances database, parsing the serialized instance protobuf, acquiring a
      missing service, launching a service — is wrapped defensively. Any failure
      (missing table/db, parse error, no rows, no uri, no source-application
      configured, launch error) degrades to ``None``.
    * A ``None`` return means "no reachable endpoint for this core service"; the
      caller is expected to fall back to its existing behaviour rather than assume
      the service is available.
    * No new gRPC/gas/storage logic is introduced. Downloading reuses
      :func:`src.core_services.source_application.acquire_service` and launching
      reuses the existing ``nodo execute`` path
      (:func:`src.commands.execute.execute`).
"""

import sqlite3
from typing import Dict, Optional, Tuple

from protos import celaut_pb2 as celaut
from src.utils.config import ConfigManager

_env_manager = ConfigManager()


def _find_running_endpoint(service_id: str) -> Optional[str]:
    """Return the first ``http://<ip>:<port>`` of a running instance of ``service_id``.

    Queries the ``local_instances`` table for rows matching ``service_id``, parses each
    ``serialized_instance`` (a :class:`celaut.Instance` protobuf) and returns the first
    ``uri_slot[*].uri[*]`` rendered as an HTTP endpoint. Returns ``None`` when no such
    instance is running or its endpoint cannot be determined.

    Fully defensive: a missing database/table, an unparseable serialized instance, no
    matching rows, or an instance without any uri all yield ``None`` — this never raises.
    """
    try:
        database_file = _env_manager.get("DATABASE_FILE")
        if not database_file:
            return None

        conn = sqlite3.connect(database_file)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT serialized_instance FROM local_instances WHERE service_id = ?",
                (service_id,),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
    except Exception:
        # Missing db/table, locked db, bad query, etc. — fail closed.
        return None

    for row in rows:
        serialized_instance = row[0] if row else None
        if not serialized_instance:
            continue
        try:
            instance = celaut.Instance()
            instance.ParseFromString(serialized_instance)
            for _exp in instance.uri_slot:
                for _uri in _exp.uri:
                    ip = str(_uri.ip or "").strip()
                    port = _uri.port
                    if ip and port:
                        return f"http://{ip}:{port}"
        except Exception:
            # Unparseable blob for this row — try the next one.
            continue

    return None


def find_running_instance(service_id: str) -> Optional[str]:
    """Return ``instance_token`` for a running instance of ``service_id``.

    Returns ``None`` when no instance of ``service_id`` is running or its row can't be
    read. Fully defensive: a missing database/table, an unparseable serialized
    instance, or no matching rows all yield ``None`` — this never raises.
    """
    try:
        database_file = _env_manager.get("DATABASE_FILE")
        if not database_file:
            return None

        conn = sqlite3.connect(database_file)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, serialized_instance FROM local_instances WHERE service_id = ?",
                (service_id,),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
    except Exception:
        # Missing db/table, locked db, bad query, etc. — fail closed.
        return None

    for row in rows:
        token = str(row[0]) if row and row[0] else None
        if not token:
            continue
        return token

    return None


def ensure_core_service_running(
    service_id: str,
    *,
    launch: bool = True,
    envs: Optional[Dict[str, str]] = None,
    source_url: Optional[str] = None,
) -> Optional[str]:
    """Ensure a core service is running and return its endpoint, or ``None``.

    Resolution order:
        (a) If an instance of ``service_id`` is already running, return its endpoint.
        (b) Otherwise, best-effort *download* the service into the local registry so it
            can be launched. When ``source_url`` is provided, the service is fetched
            directly from that manifest URL (bypassing the source-application lookup);
            otherwise it is resolved via the source-application
            (:func:`acquire_service`).
        (c) Otherwise (when ``launch`` is ``True``), best-effort *launch* the service
            by id by reusing the existing ``nodo execute`` path
            (:func:`src.commands.execute.execute`), injecting ``envs`` into the new
            instance's environment (e.g. ``SOURCE_SIGNER_MODE``/``SOURCE_MNEMONIC`` to
            run the source-application as an autonomous seed signer).

    Note: ``envs`` only applies to a *newly launched* instance (c). If an instance is
    already running (a), its existing environment is used as-is — this never relaunches
    to change env.

    After a download and/or launch attempt, :func:`find_running_endpoint` is re-checked
    and its result returned.

    Launch wiring: launching reuses ``nodo execute`` rather than re-implementing the
    gRPC/gas launch logic. ``execute()`` is side-effectful (it prints a launch animation
    and performs a blocking gRPC call against the local gateway daemon), so the call is
    wrapped in a broad ``try/except`` — ANY failure (no gateway, missing optional
    dependency, gRPC error, ``SystemExit``) degrades to ``None`` and never propagates to
    the caller. ``launch=False`` lets a caller resolve-and-download only (e.g. to check
    availability) without triggering a launch.
    """
    # (a) Already running?
    endpoint = _find_running_endpoint(service_id)
    if endpoint:
        return endpoint

    # (b) Not running — make sure it's at least present locally (best-effort download).
    #     A direct ``source_url`` takes precedence: fetch the service straight from that
    #     manifest URL, bypassing the source-application lookup. When it is empty, resolve
    #     the sources via the source-application (:func:`acquire_service`). Both paths are
    #     fail-closed: a download failure must not break the ensure path.
    try:
        if source_url and source_url.strip():
            from src.commands.publisher.publisher import download_from_manifest_url

            download_from_manifest_url(source_url.strip())
        else:
            from src.core_services.source_application import acquire_service

            acquire_service(service_id)
    except Exception:
        # Defensive: a download failure must not break the ensure path.
        pass

    # (c) Still not running — attempt to launch via the existing execute path.
    if launch:
        try:
            from src.commands.execute import execute

            # Reuse the canonical launch path; broadly guarded because execute() is
            # side-effectful and depends on a running gateway daemon. BaseException is
            # caught so a stray SystemExit from the execute path can't escape either.
            execute(service_id, envs=envs)
        except BaseException:
            # Any launch failure → fall through and re-check below; never raise.
            pass

    # Re-check for a now-running instance after acquire/launch.
    return _find_running_endpoint(service_id)

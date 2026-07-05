"""
Resource accounting glue for virtiofs shared-disk networks.

The pure attribution math (measure ``du`` of the shared dirs a service
originated, clamp to the origin's declared ``disk_space``) lives in
``src.virtualizers.ch.virtiofs.attributed_shared_disk_usage_bytes`` so it stays
directly unit-testable. This module adds the persistence glue: resolve which
networks an *instance* originated (from the ``network_origins`` table) and read
its declared ``disk_space`` from ``local_instances``.

Imports are deferred into the function so importing this module stays cheap and
does not drag in the full CH/Docker runtime — matching how the node's pure
helpers are kept isolated from the heavy virtualizer package ``__init__``.
"""
from typing import Optional


def origin_instance_shared_disk_usage_bytes(
    instance_id: str,
    *,
    base_dir: str,
    sc=None,
    declared_disk_space: Optional[int] = None,
) -> int:
    """
    Shared-disk usage attributed to a single origin *instance*, capped by that
    instance's declared ``disk_space``.

    Sums the measured usage of the shared directories of every network the
    instance originated (persisted in ``network_origins``) and clamps it to the
    instance's declared ``disk_space`` — the hard ceiling. Used wherever the
    node counts/reports an instance's disk consumption (see
    ``manager.get_sysresources``).

    ``sc`` may be injected (an ``SQLConnection``); defaults to the singleton.
    ``declared_disk_space`` may be injected to skip a second DB read; otherwise
    it is read from the instance's ``local_instances`` row.
    """
    from src.virtualizers.ch.virtiofs import attributed_shared_disk_usage_bytes

    if sc is None:
        from src.database.sql_connection import SQLConnection
        sc = SQLConnection()

    if declared_disk_space is None:
        try:
            row = sc.get_sys_req(id=instance_id)
            declared_disk_space = row["disk_space"] if row is not None else None
        except Exception:
            declared_disk_space = None

    network_ids = sc.get_origin_networks(instance_id)
    return attributed_shared_disk_usage_bytes(
        network_ids,
        base_dir=base_dir,
        declared_disk_space=declared_disk_space,
    )

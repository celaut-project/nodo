"""``nodo hibernate <instance> [--json]`` -- put one instance in hibernation, as the operator.

The VM stops and its disk, row, address and account are kept (``src/manager/hibernation.py``).
Only a resize that gives it CPU and RAM again wakes it: a top-up does not. A service that
declared a notice gets it first (#645), as for ``nodo kill``.

The operator pays nothing for this and the instance is not charged: it is not the
``ModifyServiceSystemResources`` of a client or of the instance. The ledger of the instance
gets a ``resource_change`` row that moves nothing, with ``detail.actor`` ``operator``, so
the statement of the instance says who put it down.

Stopping the VM needs root or ``CAP_NET_ADMIN``, as ``nodo kill`` does.
"""

from typing import Any, Dict

from src.database import ledger
from src.database.sql_connection import SQLConnection
from src.manager.manager import resolve_instance_token
from src.utils.privileges import can_admin_network

sc = SQLConnection()


def hibernate(instance: str, as_json: bool = False) -> bool:
    from src.commands._catalogue import emit_error, emit_json
    from src.manager import hibernation, stopping

    # The operator can type a name; the manager takes only the token.
    token = resolve_instance_token(reference=instance) or instance
    if not sc.internal_instance_exists(id=token):
        return emit_error(as_json, f"This node has no local instance {instance}.")
    if sc.is_local_instance_hibernated(id=token):
        message = f"Instance {instance} is already hibernated."
        if as_json:
            emit_json({"hibernated": token, "already": True})
        else:
            print(message, flush=True)
        return True
    if not can_admin_network():
        return emit_error(
            as_json,
            "Hibernating an instance needs root or CAP_NET_ADMIN, as stopping it does. "
            "Run with sudo.",
        )
    try:
        done = stopping.request_hibernation(token)
    except hibernation.HibernationRefused as refusal:
        return emit_error(as_json, str(refusal))
    except Exception as error:
        return emit_error(as_json, f"Something was wrong: {error}")
    if not done:
        return emit_error(as_json, "Something was wrong.")

    sc.move_balance(
        token, 0, ledger.RESOURCE_CHANGE,
        detail={"actor": ledger.ACTOR_OPERATOR, "action": "hibernate"}, write_zero=True,
    )
    if as_json:
        document: Dict[str, Any] = {"hibernated": token, "already": False}
        emit_json(document)
    else:
        print(f"Instance {instance} hibernated: its disk is kept.", flush=True)
    return True

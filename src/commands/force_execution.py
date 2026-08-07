"""
`nodo force_execution <peer_id> <service>` -- testing/dev only.

`nodo execute` never lets the caller pick who runs the service: delegation is
fully automatic (`execution_balancer` queries `local` and every connected peer,
sorts by cost, and `launch_service` tries candidates cheapest-first). That makes
peer-to-peer delegation and tunneling hard to test deterministically.

This command bypasses the balancer and delegates straight to a named peer: no
cost comparison against `local` or any other peer, no cheapest-first fallback
if it fails. It still goes through the normal cost/gas accounting for the
delegated instance (the peer's own `GetServiceEstimatedCost`, `spend_gas`,
`gas_amount_on_other_peer`) -- only peer *selection* is skipped.

The bypass is server-side (`launch_service._force_delegate`), correlated via a
one-time token this command generates and stores against the peer id
(`SQLConnection.set_forced_execution_peer`), sent as this call's
`recursion_guard_token` so the gateway can look it up and consume it. See
`launch_service.py` for why it's keyed by that token and not by client id.
"""
import uuid

from protos import celaut_pb2

from src.commands.execute import (
    launch_via_gateway,
    acquire_service,
    print_endpoints,
    resolve_service_hash,
)
from src.database.sql_connection import SQLConnection
from src.manager.manager import get_execute_client
from src.utils.hashing import get_configured_hash_id
from src.utils.config import ConfigManager
from src.utils.instance_names import inject_instance_name
from src.utils.utils import to_gas_amount

env_manager = ConfigManager()

CONFIGURED_HASH_ID = get_configured_hash_id(env_manager)

sc = SQLConnection()


def _forced_generator(
    _hash: str,
    token: str,
    initial_gas_amount: int,
    envs: dict[str, str] | None = None,
    instance_name: str | None = None,
):
    try:
        client_id = get_execute_client(gas_amount=initial_gas_amount, external=False)
    except Exception:
        raise RuntimeError("No execute client available.")

    try:
        yield celaut_pb2.Client(client_id=client_id)
        # Correlates this call with the forced-peer hint stored under `token`
        # (see `SQLConnection.set_forced_execution_peer`) -- `RecursionGuard`
        # uses a caller-supplied token as-is rather than generating its own.
        yield celaut_pb2.RecursionGuard(token=token)

        config = celaut_pb2.Configuration(
            initial_gas_amount=to_gas_amount(initial_gas_amount)
        )
        if envs:
            config.environment_variables.update({
                k: v.encode() for k, v in envs.items()
            })
        inject_instance_name(config=config, instance_name=instance_name)
        yield config

        yield celaut_pb2.Metadata.HashTag.Hash(
                type=CONFIGURED_HASH_ID,
                value=bytes.fromhex(_hash)
            )

    except Exception as e:
        raise RuntimeError(f"Exception on forcing execution of {_hash[:6]}: {e}") from e


def force_execution(
    peer_id: str,
    service: str,
    envs: dict[str, str] | None = None,
    instance_name: str | None = None,
):
    if not sc.peer_exists(peer_id):
        print(f"❌ Peer '{peer_id}' is not connected. Check `nodo peers`.")
        return

    resolved = resolve_service_hash(service)
    if not resolved:
        if acquire_service(service):
            resolved = resolve_service_hash(service)

    if not resolved:
        print("❌ Service not allowed.")
        return

    service = resolved

    token = uuid.uuid4().hex
    sc.set_forced_execution_peer(token=token, peer_id=peer_id)
    try:
        response = launch_via_gateway(
            service=service,
            input_generator=_forced_generator(
                _hash=service,
                token=token,
                initial_gas_amount=10**16,
                envs=envs,
                instance_name=instance_name,
            ),
            success_message=f"🚀 Service forced onto peer {peer_id} successfully!\n",
        )
    finally:
        # Best-effort cleanup: `launch_service` already consumes (pops) the hint
        # on the success path, so this is only ever a real delete when the call
        # never reached it (e.g. the gateway wasn't reachable at all). Harmless
        # either way -- the token is single-use and never read again.
        sc.pop_forced_execution_peer(token)

    if response is None:
        return

    print_endpoints(response)

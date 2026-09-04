from hashlib import sha256
from typing import Callable, List

from bee_rpc import client as bee

from src.utils.config import ConfigManager

from protos import celaut_pb2, celaut_pb2_grpc
from protos.gateway_bee import StartService_input_indices
from src.manager.manager import get_client_id_on_other_peer
from src.manager.metrics import balance_on_other_peer
from src.database.sql_connection import SQLConnection
from src.tunneling import delegated_endpoints
from src.utils import utils, logger as log
from src.identity.grpc_transport import peer_channel
from src.utils.monetary import format_mu
from src.payment_system.mu_conversion import (
    configuration_for_peer,
    matching_payment_system,
)


env_manager = ConfigManager()
START_SERVICE_ON_PEER_TIMEOUT = int(env_manager.get("START_SERVICE_ON_PEER_TIMEOUT"))


def _publish_locally_if_needed(
        external_token: str,
        peer: str,
        instance: celaut_pb2.Instance,
        father_id: str,
        father_ip: str,
) -> celaut_pb2.Instance:
    """Return the instance to hand to our client, tunnelled through us if needed."""
    if not delegated_endpoints.should_tunnel(instance):
        return instance

    advertise_ip = delegated_endpoints.advertise_ip_for(father_id=father_id, father_ip=father_ip)
    if not advertise_ip:
        log.LOGGER(
            'Delegated instance needs a local tunnel endpoint but no address of ours '
            f'is usable for {father_id or father_ip}; handing over the peer addresses.'
        )
        return instance

    try:
        peer_gateway = next(utils.generate_uris_by_peer_id(peer))
    except StopIteration:
        log.LOGGER(f'No reachable gateway address for peer {peer}; cannot tunnel.')
        return instance

    return delegated_endpoints.publish(
        token=external_token,
        peer_gateway=peer_gateway,
        instance=instance,
        bind_ip=advertise_ip,
        peer_id=peer,
    )

def delegate_execution(
                        service_id: str,
                        peer: str,
                        father_id: str,
                        cost: int, metadata, config,
                        recursion_guard_token,
                        refund_container: List[Callable],
                        father_ip: str = ""
                   ) -> celaut_pb2.ServiceInstance:
    try:
        log.LOGGER('The service is launched on node ' + str(peer))

        # The configuration travels to the peer's Gateway, which reads MU on its
        # own scale, so it is translated here. The cost is not: `cost` and
        # `balance_on_other_peer` are both already in our MU (the balance is
        # converted where it enters the node, in `manager.metrics`), and
        # converting one side of a comparison whose other side is local was how
        # this check came to compare two different scales.
        payment_system = matching_payment_system(peer)
        peer_config = configuration_for_peer(config, payment_system=payment_system)

        if balance_on_other_peer(peer_id=peer) <= cost:
            raise Exception(
                'Launch service error: not enough balance on ' + peer + '. '
                'Current: ' + format_mu(balance_on_other_peer(peer_id=peer)) + ', required: ' + format_mu(cost) + '.'
            )

        log.LOGGER('Go to launch the service on ' + str(peer))
        service_instance = next(bee.client_grpc(
            method=celaut_pb2_grpc.GatewayStub(
                peer_channel(peer)
            ).StartService,
            timeout=START_SERVICE_ON_PEER_TIMEOUT if START_SERVICE_ON_PEER_TIMEOUT > 0 else None,
            partitions_message_mode_parser=True,
            indices_serializer=StartService_input_indices,
            indices_parser=celaut_pb2.ServiceInstance,
            input=utils.service_extended(
                metadata=metadata,
                config=peer_config,
                # TODO: Could pass only the previously selected configuration with the estimate cost
                #  request, now is allowing to select another (that could be reasonable).
                client_id=get_client_id_on_other_peer(peer_id=peer),
                recursion_guard_token=recursion_guard_token
            )
        ))
        external_token: str = service_instance.token
        encrypted_external_token: str = sha256(external_token.encode('utf-8')).hexdigest()

        # The peer advertised its own addresses. If our client cannot reach them,
        # stand in for the service locally and hand over our endpoints instead —
        # the client speaks the service's protocol, not beeRPC, so only this node
        # can front the tunnel for it. Policy: network.DELEGATION_TUNNEL_POLICY.
        # TODO adapt for ipv6 too.
        published_instance = _publish_locally_if_needed(
            external_token=external_token,
            peer=peer,
            instance=service_instance.instance,
            father_id=father_id,
            father_ip=father_ip,
        )

        SQLConnection().add_delegated_instance(
            father_id=father_id,
            peer_id=peer,  # Add node_uri.
            encrypted_external_token=encrypted_external_token,  # Add token.
            external_token=external_token,
            # Store what the client is told, so tunnels can be restored after a
            # restart and firewall cleanup targets the address actually handed out.
            serialized_instance=published_instance.SerializeToString(),
            service_id=service_id
        )
        service_instance.token = encrypted_external_token
        service_instance.instance.CopyFrom(published_instance)
        return service_instance
    except Exception as e:
        log.LOGGER('Failed starting a service on peer, occurs the error: ' + str(e))
        try:
            refund_container.pop()()
        except IndexError:
            pass
        raise e

from bee_rpc import client as bee
import grpc

from protos import celaut_pb2_grpc, celaut_pb2
from src.gateway.iterables.estimated_cost_iterable import GetServiceEstimatedCostIterable
from src.gateway.iterables.get_service_iterable import GetServiceIterable
from src.gateway.iterables.observe_iterable import ObserveIterable
from src.gateway.iterables.start_service_iterable import StartServiceIterable
from src.utils.contract_xattrs import get_script, get_contract_type
from src.tunneling.rpc_tunnel import TunnelError, service_tunnel
from src.gateway.utils import generate_full_node_peer_info
from src.manager.manager import add_peer_instance, modify_deposit, stop_instance, generate_client, get_internal_service_id_by_uri, spend_mu, \
    hotplug, get_sysresources
from src.manager.metrics import get_metrics
from src.payment_system.payment_process import generate_deposit_token, validate_payment_process
from src.utils import logger as log
from src.utils.utils import from_amount, get_only_the_ip_from_context, to_amount
from src.utils.config import ConfigManager
from src.utils.monetary import prices

env_manager = ConfigManager()


class Gateway(celaut_pb2_grpc.Gateway):

    def GetServiceEstimatedCost(self, request_iterator, context, **kwargs):
        yield from GetServiceEstimatedCostIterable(request_iterator, context)

    def StartService(self, request_iterator, context, **kwargs):
        yield from StartServiceIterable(request_iterator, context)

    def StopService(self, request_iterator, context, **kwargs):
        try:
            log.LOGGER('Stopping instance.')
            token = next(bee.parse_from_buffer(
                                request_iterator=request_iterator,
                                indices=celaut_pb2.TokenMessage,
                                partitions_message_mode=True
                            ), 0).token
            log.LOGGER(f'    with id {token}')
            refunded_amount = stop_instance(token=token)
            if not refunded_amount: refunded_amount = 0
            
            log.LOGGER(f'Stopped instance {token}.')
            yield from bee.serialize_to_buffer(
                    message_iterator=celaut_pb2.Refund(
                        amount=to_amount(refunded_amount)
                    )
            )
        except Exception as e:
            raise Exception('Was imposible stop the service. ' + str(e))

    def ModifyDeposit(self, request_iterator, context, **kwargs):
        try:
            log.LOGGER('Modifying deposit on service.')

            _input = next(bee.parse_from_buffer(
                                request_iterator=request_iterator,
                                indices=celaut_pb2.ModifyDepositInput,
                                partitions_message_mode=True
                            ), 0)

            success, message = modify_deposit(
                        amount_mu=from_amount(_input.difference),
                        service_token=_input.service_token
                    )

            log.LOGGER(f"Message on modify deposit: {message}")

            yield from bee.serialize_to_buffer(
                    message_iterator=celaut_pb2.ModifyDepositOutput(
                        success=success,
                        message=message
                    )
            )
        except Exception as e:
            raise Exception('Was imposible stop the service. ' + str(e))

    def GetPeerInfo(self, request_iterator, context, **kwargs):
        log.LOGGER(f'Request for instance by {context.peer()}')
        gateway_instance = generate_full_node_peer_info()
        yield from bee.serialize_to_buffer(gateway_instance)

    def IntroducePeer(self, request_iterator, context, **kwargs):
        # TODO DDOS protection.   ¿?
        log.LOGGER('Introduce peer method.')
        add_peer_instance(
                peer=next(bee.parse_from_buffer(
                request_iterator=request_iterator,
                indices=celaut_pb2.Peer,
                partitions_message_mode=True
            ), None)
        )

        yield from bee.serialize_to_buffer(celaut_pb2.RecursionGuard(token="OK"))  # Recursion guard shouldn't be used here, another message should be used. TODO

    def GenerateClient(self, request_iterator, context, **kwargs):
        # TODO DDOS protection.   ¿?
        yield from bee.serialize_to_buffer(
                message_iterator=generate_client()
        )

    def GenerateDepositToken(self, request_iterator, context, *kwargs):
        yield from bee.serialize_to_buffer(
                message_iterator=celaut_pb2.TokenMessage(
                    token=generate_deposit_token(
                        client_id=next(bee.parse_from_buffer(
                            request_iterator=request_iterator,
                            indices=celaut_pb2.Client,
                            partitions_message_mode=True
                        ), 0).client_id
                    )
                )
        )

    def ModifyServiceSystemResources(self, request_iterator, context, **kwargs):
        log.LOGGER('Request for modify service system resources.')
        token = get_internal_service_id_by_uri(uri=get_only_the_ip_from_context(context_peer=context.peer()))
        refund_container = []
        if not spend_mu(
                id=token,
                amount_mu=prices().modify_resources_mu,
                refund_function_container=refund_container
        ): raise Exception('Error charging for the resource change of ' + context.peer())
        if not hotplug(
                vmachine_id=token,
                system_requeriments_range=next(bee.parse_from_buffer(
                    request_iterator=request_iterator,
                    indices=celaut_pb2.ModifyServiceSystemResourcesInput,
                    partitions_message_mode=True
                ), None)
        ):
            try:
                refund_container.pop()()
            except IndexError:
                pass
            raise Exception('Exception on service modify method.')

        yield from bee.serialize_to_buffer(
                message_iterator=get_sysresources(id=token)
        )

    def GetService(self, request_iterator, context, **kwargs):
        yield from GetServiceIterable(request_iterator, context)

    def Payable(self, request_iterator, context, **kwargs):
        log.LOGGER('Request for payment.')
        payment = next(bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=celaut_pb2.Payment,
            partitions_message_mode=True
        ), None)
        raw_script = get_script(payment.contract)
        # Select the payment validator by the stable, wallet-independent contract_type; the
        # raw ErgoTree/propositionBytes travels as ``script`` (never a textual address).
        contract_type = get_contract_type(payment.contract) or raw_script
        if not validate_payment_process(
                amount=from_amount(payment.amount),
                ledger=payment.contract.ledger,
                contract=contract_type,
                script=raw_script,
                token=payment.deposit_token,
        ):
            raise Exception('Error: payment not valid.')
        log.LOGGER('Payment is valid.')
        for b in bee.serialize_to_buffer(): yield b

    def GetMetrics(self, request_iterator, context, **kwargs):
        yield from bee.serialize_to_buffer(
                message_iterator=get_metrics(
                    token=next(bee.parse_from_buffer(
                        request_iterator=request_iterator,
                        indices=celaut_pb2.TokenMessage,
                        partitions_message_mode=True
                    ), None).token
                ),
                indices=celaut_pb2.Metrics,
        )

    def ServiceTunnel(self, request_iterator, context, **kwargs):
        try:
            # The stream carries two message types: the leading TokenMessage
            # handshake (index 1) and the raw payload (index 0). Both are parsed
            # in memory — `partitions_message_mode=False` would spill every
            # payload chunk to a temporary file, which no byte pipe can afford.
            conn, relay = service_tunnel(
                iterator=bee.parse_from_buffer(
                    request_iterator=request_iterator,
                    indices={1: celaut_pb2.TokenMessage, 0: bytes},
                    partitions_message_mode={1: True, 0: True}
                ),
                is_active=context.is_active,
            )
        except TunnelError as e:
            log.LOGGER(f'Tunnel refused: {e}')
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
            return

        try:
            yield from bee.serialize_to_buffer(
                    message_iterator=relay,
                    # Mirrors the input map. Declaring a second index also keeps
                    # bee_rpc from inferring the index off the first message, which
                    # it does by calling next() unguarded — a service that closes
                    # without replying would surface as a RuntimeError instead of an
                    # empty stream.
                    indices={1: celaut_pb2.TokenMessage, 0: bytes},
            )
        finally:
            # The socket is opened eagerly inside service_tunnel; guarantee it is
            # released even if serialize_to_buffer bails before the relay
            # generator is ever iterated (its own finally would not run then).
            relay.close()
            try:
                conn.close()
            except OSError:
                pass

    def Observe(self, request_iterator, context, **kwargs):
        yield from ObserveIterable(request_iterator, context)

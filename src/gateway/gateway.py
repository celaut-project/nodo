from bee_rpc import client as bee

from protos import gateway_pb2_grpc, gateway_pb2
from src.packers.zip_with_dockerfile import pack_zip
from src.gateway.iterables.estimated_cost_iterable import GetServiceEstimatedCostIterable
from src.gateway.iterables.get_service_iterable import GetServiceIterable
from src.gateway.iterables.start_service_iterable import StartServiceIterable
from src.reputation_system.contracts.ergo.proof_validation import sign_message
from src.tunneling_system.rpc_tunnel import service_tunnel
from src.tunneling_system.tunnels import TunnelSystem
from src.gateway.utils import generate_gateway_instance
from src.manager.manager import add_peer_instance, modify_gas_deposit, prune_container, generate_client, get_internal_service_id_by_uri, spend_gas, \
    container_modify_system_params, get_sysresources
from src.manager.metrics import get_metrics
from src.payment_system.payment_process import generate_deposit_token, validate_payment_process
from src.utils import logger as log
from src.utils.utils import from_gas_amount, get_only_the_ip_from_context, to_gas_amount, get_network_name
from src.utils.env import EnvManager

env_manager = EnvManager()

MODIFY_SERVICE_SYSTEM_RESOURCES_COST = env_manager.get_env("MODIFY_SERVICE_SYSTEM_RESOURCES_COST")
GAS_COST_FACTOR = env_manager.get_env("GAS_COST_FACTOR")


class Gateway(gateway_pb2_grpc.Gateway):

    def StartService(self, request_iterator, context, **kwargs):
        yield from StartServiceIterable(request_iterator, context)

    def StopService(self, request_iterator, context, **kwargs):
        try:
            log.LOGGER('Stopping service.')
            yield from bee.serialize_to_buffer(
                    message_iterator=gateway_pb2.Refund(
                        amount=to_gas_amount(prune_container(
                            token=next(bee.parse_from_buffer(
                                request_iterator=request_iterator,
                                indices=gateway_pb2.TokenMessage,
                                partitions_message_mode=True
                            ), 0).token
                        ))
                    )
            )
        except Exception as e:
            raise Exception('Was imposible stop the service. ' + str(e))

    def ModifyGasDeposit(self, request_iterator, context, **kwargs):
        try:
            log.LOGGER('Modifying gas deposit on service.')

            _input = next(bee.parse_from_buffer(
                                request_iterator=request_iterator,
                                indices=gateway_pb2.ModifyGasDepositInput,
                                partitions_message_mode=True
                            ), 0)

            success, message = modify_gas_deposit(
                        gas_amount=from_gas_amount(_input.gas_difference),
                        service_token=_input.service_token
                    )

            log.LOGGER(f"Message on modify gas deposit: {message}")

            yield from bee.serialize_to_buffer(
                    message_iterator=gateway_pb2.ModifyGasDepositOutput(
                        success=success,
                        message=message
                    )
            )
        except Exception as e:
            raise Exception('Was imposible stop the service. ' + str(e))

    def GetInstance(self, request_iterator, context, **kwargs):
        log.LOGGER(f'Request for instance by {context.peer()}')
        ip = get_only_the_ip_from_context(context_peer=context.peer())
        if TunnelSystem().from_tunnel(ip=ip):
            gateway_instance = TunnelSystem().get_gateway_tunnel()
        else:
            gateway_instance = generate_gateway_instance(
                network=get_network_name(direction=ip)
            )
        yield from bee.serialize_to_buffer(gateway_instance)

    def IntroducePeer(self, request_iterator, context, **kwargs):
        # TODO DDOS protection.   ¿?
        log.LOGGER('Introduce peer method.')
        add_peer_instance(
                peer=next(bee.parse_from_buffer(
                request_iterator=request_iterator,
                indices=gateway_pb2.Instance,
                partitions_message_mode=True
            ), None)
        )

        yield from bee.serialize_to_buffer(gateway_pb2.RecursionGuard(token="OK"))  # Recursion guard shouldn't be used here, another message should be used. TODO

    def GenerateClient(self, request_iterator, context, **kwargs):
        # TODO DDOS protection.   ¿?
        yield from bee.serialize_to_buffer(
                message_iterator=generate_client()
        )

    def GenerateDepositToken(self, request_iterator, context, *kwargs):
        yield from bee.serialize_to_buffer(
                message_iterator=gateway_pb2.TokenMessage(
                    token=generate_deposit_token(
                        client_id=next(bee.parse_from_buffer(
                            request_iterator=request_iterator,
                            indices=gateway_pb2.Client,
                            partitions_message_mode=True
                        ), 0).client_id
                    )
                )
        )

    def ModifyServiceSystemResources(self, request_iterator, context, **kwargs):
        log.LOGGER('Request for modify service system resources.')
        token = get_internal_service_id_by_uri(uri=get_only_the_ip_from_context(context_peer=context.peer()))
        refund_gas = []
        if not spend_gas(
                id=token,
                gas_to_spend=MODIFY_SERVICE_SYSTEM_RESOURCES_COST * GAS_COST_FACTOR,
                refund_gas_function_container=refund_gas
        ): raise Exception('Launch service error spending gas for ' + context.peer())
        if not container_modify_system_params(
                id=token,
                system_requeriments_range=next(bee.parse_from_buffer(
                    request_iterator=request_iterator,
                    indices=gateway_pb2.ModifyServiceSystemResourcesInput,
                    partitions_message_mode=True
                ), None)
        ):
            try:
                refund_gas.pop()()
            except IndexError:
                pass
            raise Exception('Exception on service modify method.')

        yield from bee.serialize_to_buffer(
                message_iterator=get_sysresources(
                    id=token
                )
        )

    def Pack(self, request_iterator, context, **kwargs):
        log.LOGGER('Go to pack a proyect.')
        _d: bee.Dir = next(bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices={0: bytes},
            partitions_message_mode={0: False}
        ), None)
        if _d.type != bytes:
            raise Exception("Incorrect input on Pack grpc method. Should be bytes")
        yield from pack_zip(zip=_d.dir)

    def GetServiceEstimatedCost(self, request_iterator, context, **kwargs):
        yield from GetServiceEstimatedCostIterable(request_iterator, context)

    def GetService(self, request_iterator, context, **kwargs):
        log.LOGGER(f"Get service method.")
        yield from GetServiceIterable(request_iterator, context)

    def Payable(self, request_iterator, context, **kwargs):
        log.LOGGER('Request for payment.')
        payment = next(bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=gateway_pb2.Payment,
            partitions_message_mode=True
        ), None)
        if not validate_payment_process(
                amount=from_gas_amount(payment.gas_amount),
                ledger=payment.contract_ledger.ledger,
                contract=payment.contract_ledger.contract,
                contract_addr=payment.contract_ledger.contract_addr,
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
                        indices=gateway_pb2.TokenMessage,
                        partitions_message_mode=True
                    ), None).token
                ),
                indices=gateway_pb2.Metrics,
        )

    def ServiceTunnel(self, request_iterator, context, **kwargs):
        yield from bee.serialize_to_buffer(
                message_iterator=service_tunnel(
                    iterator=bee.parse_from_buffer(
                        request_iterator=request_iterator,
                        indices={0: bytes},
                        partitions_message_mode={0: False}
                    )
                ),
                indices=gateway_pb2.Metrics,
        )

    def SignPublicKey(self, request_iterator, context, **kwargs):
        try:
            log.LOGGER('Signing public key.')
            
            # Parse the input from the request iterator
            sign_request = next(bee.parse_from_buffer(
                request_iterator=request_iterator,
                indices=gateway_pb2.SignRequest,
                partitions_message_mode=True
            ), None)
            
            if sign_request is None:
                raise Exception("Invalid input for SignPublicKey method.")
            
            # Use the sign_message method to sign the public key
            signed_message = sign_message(
                public_key=sign_request.public_key,
                message=sign_request.to_sign
            )
            
            # Create the response
            sign_response = gateway_pb2.SignResponse(
                signed=signed_message
            )
            
            # Serialize and yield the response
            yield from bee.serialize_to_buffer(
                message_iterator=sign_response
            )
            
        except Exception as e:
            raise Exception('Error in SignPublicKey method: ' + str(e))
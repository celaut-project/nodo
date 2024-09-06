from grpcbigbuffer import client as grpcbf

from protos import gateway_pb2_grpc, gateway_pb2
from src.compiler.compile import compile_zip
from src.gateway.iterables.estimated_cost_iterable import GetServiceEstimatedCostIterable
from src.gateway.iterables.get_service_iterable import GetServiceIterable
from src.gateway.iterables.start_service_iterable import StartServiceIterable
from src.tunneling_system.tunnels import TunnelSystem
from src.gateway.utils import generate_gateway_instance
from src.manager.manager import prune_container, generate_client, get_token_by_uri, spend_gas, \
    container_modify_system_params, get_sysresources
from src.manager.metrics import get_metrics
from src.payment_system.payment_process import validate_payment_process
from src.utils import logger as l
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
            l.LOGGER('Stopping service.')
            yield from grpcbf.serialize_to_buffer(
                    message_iterator=gateway_pb2.Refund(
                        amount=to_gas_amount(prune_container(
                            token=next(grpcbf.parse_from_buffer(
                                request_iterator=request_iterator,
                                indices=gateway_pb2.TokenMessage,
                                partitions_message_mode=True
                            )).token
                        ))
                    )
            )
        except Exception as e:
            raise Exception('Was imposible stop the service. ' + str(e))

    def GetInstance(self, request_iterator, context, **kwargs):
        l.LOGGER(f'Request for instance by {context.peer()}')
        ip = get_only_the_ip_from_context(context_peer=context.peer())
        if TunnelSystem().from_tunnel(ip=ip):
            gateway_instance = TunnelSystem().get_gateway_tunnel()
        else:
            gateway_instance = generate_gateway_instance(
                network=get_network_name(ip_or_uri=ip)
            )
        yield from grpcbf.serialize_to_buffer(gateway_instance)

    def GenerateClient(self, request_iterator, context, **kwargs):
        # TODO DDOS protection.   ¿?
        yield from grpcbf.serialize_to_buffer(
                message_iterator=generate_client()
        )

    def ModifyServiceSystemResources(self, request_iterator, context, **kwargs):
        l.LOGGER('Request for modify service system resources.')
        token = get_token_by_uri(
            uri=get_only_the_ip_from_context(context_peer=context.peer())
        )
        refund_gas = []
        if not spend_gas(
                id=token,
                gas_to_spend=MODIFY_SERVICE_SYSTEM_RESOURCES_COST * GAS_COST_FACTOR,
                refund_gas_function_container=refund_gas
        ): raise Exception('Launch service error spending gas for ' + context.peer())
        if not container_modify_system_params(
                token=token,
                system_requeriments_range=next(grpcbf.parse_from_buffer(
                    request_iterator=request_iterator,
                    indices=gateway_pb2.ModifyServiceSystemResourcesInput,
                    partitions_message_mode=True
                ))
        ):
            try:
                refund_gas.pop()()
            except IndexError:
                pass
            raise Exception('Exception on service modify method.')

        yield from grpcbf.serialize_to_buffer(
                message_iterator=get_sysresources(
                    token=token
                )
        )

    def Compile(self, request_iterator, context, **kwargs):
        l.LOGGER('Go to compile a proyect.')
        _d: grpcbf.Dir = next(grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices={0: bytes},
            partitions_message_mode={0: False}
        ))
        if _d.type != bytes:
            raise Exception("Incorrect input on Compile gRPC-bb method. Should be bytes")
        yield from compile_zip(zip=_d.dir)

    def GetServiceEstimatedCost(self, request_iterator, context, **kwargs):
        yield from GetServiceEstimatedCostIterable(request_iterator, context)

    def GetService(self, request_iterator, context, **kwargs):
        l.LOGGER(f"Get service method.")
        yield from GetServiceIterable(request_iterator, context)


    def Payable(self, request_iterator, context, **kwargs):
        l.LOGGER('Request for payment.')
        payment = next(grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices=gateway_pb2.Payment,
            partitions_message_mode=True
        ))
        if not validate_payment_process(
                amount=from_gas_amount(payment.gas_amount),
                ledger=payment.contract_ledger.ledger,
                contract=payment.contract_ledger.contract,
                contract_addr=payment.contract_ledger.contract_addr,
                token=payment.deposit_token,
        ):
            raise Exception('Error: payment not valid.')
        l.LOGGER('Payment is valid.')
        for b in grpcbf.serialize_to_buffer(): yield b

    def GetMetrics(self, request_iterator, context, **kwargs):
        yield from grpcbf.serialize_to_buffer(
                message_iterator=get_metrics(
                    token=next(grpcbf.parse_from_buffer(
                        request_iterator=request_iterator,
                        indices=gateway_pb2.TokenMessage,
                        partitions_message_mode=True
                    )).token
                ),
                indices=gateway_pb2.Metrics,
        )

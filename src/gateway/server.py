import os

from grpcbigbuffer import client as grpcbf

from protos import celaut_pb2 as celaut
from protos import gateway_pb2_grpc, gateway_pb2
from protos.gateway_pb2_grpcbf import StartService_input_indices, \
    StartService_input_message_mode
from src.builder import build
from src.compiler.compile import compile_zip
from src.gateway.start_service import start_service, get_from_registry, StartServiceIterable
from src.gateway.utils import save_service, generate_gateway_instance
from src.manager.manager import prune_container, generate_client, get_token_by_uri, spend_gas, \
    container_modify_system_params, get_sysresources, \
    execution_cost, default_initial_cost
from src.manager.metrics import get_metrics
from src.payment_system.payment_process import validate_payment_process
from src.utils import logger as l
from src.utils.env import DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH, SHA3_256_ID, \
    MODIFY_SERVICE_SYSTEM_RESOURCES_COST, GAS_COST_FACTOR, REGISTRY
from src.utils.tools.duplicate_grabber import DuplicateGrabber
from src.utils.utils import from_gas_amount, get_only_the_ip_from_context, to_gas_amount, get_network_name


class Gateway(gateway_pb2_grpc.Gateway):

    def StartService(self, request_iterator, context, **kwargs):
        yield from StartServiceIterable(request_iterator, context)

    def StopService(self, request_iterator, context, **kwargs):
        try:
            l.LOGGER('Stopping service.')
            for b in grpcbf.serialize_to_buffer(
                    message_iterator=gateway_pb2.Refund(
                        amount=to_gas_amount(prune_container(
                            token=next(grpcbf.parse_from_buffer(
                                request_iterator=request_iterator,
                                indices=gateway_pb2.TokenMessage,
                                partitions_message_mode=True
                            )).token
                        ))
                    )
            ): yield b
        except Exception as e:
            raise Exception('Was imposible stop the service. ' + str(e))

    def GetInstance(self, request_iterator, context, **kwargs):
        l.LOGGER('Request for instance by ' + str(context.peer()))
        for b in grpcbf.serialize_to_buffer(
                generate_gateway_instance(
                    network=get_network_name(
                        ip_or_uri=get_only_the_ip_from_context(
                            context_peer=context.peer()
                        )
                    )
                )
        ): yield b

    def GenerateClient(self, request_iterator, context, **kwargs):
        # TODO DDOS protection.   ¿?
        for b in grpcbf.serialize_to_buffer(
                message_iterator=generate_client()
        ): yield b

    def ModifyServiceSystemResources(self, request_iterator, context, **kwargs):
        l.LOGGER('Request for modify service system resources.')
        token = get_token_by_uri(
            uri=get_only_the_ip_from_context(context_peer=context.peer())
        )
        refound_gas = []
        if not spend_gas(
                token_or_container_ip=token,
                gas_to_spend=MODIFY_SERVICE_SYSTEM_RESOURCES_COST * GAS_COST_FACTOR,
                refund_gas_function_container=refound_gas
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
                refound_gas.pop()()
            except IndexError:
                pass
            raise Exception('Exception on service modify method.')

        for b in grpcbf.serialize_to_buffer(
                message_iterator=get_sysresources(
                    token=token
                )
        ): yield b

    def Compile(self, request_iterator, context, **kwargs):
        l.LOGGER('Go to compile a proyect.')
        _d: grpcbf.Dir = next(grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices={0: bytes},
            partitions_message_mode={0: False}
        ))
        if _d.type != bytes:
            raise Exception("Incorrect input on Compile gRPC-bb method. Should be bytes")
        for b in compile_zip(
                zip=_d.dir
        ):
            yield b

    # Estimacion de coste de ejecución de un servicio con la cantidad de gas por defecto.
    def GetServiceEstimatedCost(self, request_iterator, context, **kwargs):
        # TODO check cost in other peers (use RecursionGuard to prevent infinite loops).

        l.LOGGER('Request for the cost of a service.')
        parse_iterator = grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices=StartService_input_indices,
            partitions_message_mode=StartService_input_message_mode
        )

        client_id = None
        recursion_guard_token = None
        initial_service_cost = None
        cost = None
        initial_service_cost = None
        service_hash = None
        while True:
            try:
                r = next(parse_iterator)
            except StopIteration:
                break

            if type(r) is gateway_pb2.Client:
                client_id = r.client_id
                continue

            if type(r) is gateway_pb2.RecursionGuard:
                recursion_guard_token = r.token
                continue

            if type(r) is gateway_pb2.HashWithConfig:
                if r.HasField('initial_gas_amount'):
                    initial_service_cost = from_gas_amount(r.initial_gas_amount)
                service_hash: celaut.Any.Metadata.HashTag.Hash = r.hash
                r = service_hash

            if type(r) is celaut.Any.Metadata.HashTag.Hash and SHA3_256_ID == r.type:
                if r.value.hex() in [s for s in os.listdir(REGISTRY)]:
                    yield gateway_pb2.buffer__pb2.Buffer(signal=True)
                    try:
                        service_with_meta: gateway_pb2.ServiceWithMeta = get_from_registry(
                            service_hash=r.value.hex()
                        )
                        cost = execution_cost(
                            metadata=service_with_meta.metadata
                        ) * GAS_COST_FACTOR
                        break
                    except build.UnsupportedArchitectureException as e:
                        raise e
                    except Exception as e:
                        yield gateway_pb2.buffer__pb2.Buffer(signal=True)
                        continue
                elif DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH:
                    raise Exception("I dont've the service.")

            if r is gateway_pb2.ServiceWithMeta:
                if DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH: raise Exception("I dont've the service.")
                service_with_meta_dir, is_primary = DuplicateGrabber().next(
                    hashes=[service_hash],
                    generator=parse_iterator
                )

                save_service(
                    service_dir=service_with_meta_dir,
                    service_hash=service_hash.value.hex()
                )

                try:
                    service_with_meta: gateway_pb2.ServiceWithMeta = get_from_registry(
                        service_hash=service_hash.value.hex()
                    )
                    cost: int = execution_cost(
                        metadata=service_with_meta.metadata
                    ) * GAS_COST_FACTOR
                except build.UnsupportedArchitectureException as e:
                    raise e
                break

            if r is gateway_pb2.ServiceWithConfig:
                print('SERVICE WITH CONFIG NOT SUPPORTED')
                raise Exception('SERVICE WITH CONFIG NOT SUPPORTED')
                """"
                    if DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH: raise Exception("I dont've the service.")
                    service_with_config = next(parse_iterator)
                    initial_service_cost: int = from_gas_amount(service_with_config.initial_gas_amount)
                    service_with_meta = service_with_config.service
                    second_partition_dir = next(parse_iterator)
                    if type(second_partition_dir) is not str: raise Exception('Error: fail sending service.')
                    try:
                        cost = execution_cost(
                            service_buffer=get_service_buffer_from_registry(
                                service_hash=save_service(
                                    service_with_meta_dir=service_with_config.service,  # TODO
                                    service_hash=service_hash
                                )
                            ),
                            metadata=service_with_meta.metadata
                        ) * GAS_COST_FACTOR
                    except build.UnsupportedArchitectureException as e:
                        raise e
                    break                
                """

        if not initial_service_cost:
            initial_service_cost: int = default_initial_cost(
                father_id=client_id if client_id else get_only_the_ip_from_context(context_peer=context.peer())
            )
        cost: int = cost + initial_service_cost if cost else initial_service_cost
        l.LOGGER('Execution cost for a service is requested, cost -> ' + str(cost) + ' with benefit ' + str(0))
        if cost is None:
            raise Exception("I dont've the service.")
        for b in grpcbf.serialize_to_buffer(
                message_iterator=gateway_pb2.EstimatedCost(
                    cost=to_gas_amount(cost),
                    variance=0  # TODO dynamic variance.
                ),
                indices=gateway_pb2.EstimatedCost
        ): yield b

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
        ): raise Exception('Error: payment not valid.')
        l.LOGGER('Payment is valid.')
        for b in grpcbf.serialize_to_buffer(): yield b

    def GetMetrics(self, request_iterator, context, **kwargs):
        for b in grpcbf.serialize_to_buffer(
                message_iterator=get_metrics(
                    token=next(grpcbf.parse_from_buffer(
                        request_iterator=request_iterator,
                        indices=gateway_pb2.TokenMessage,
                        partitions_message_mode=True
                    )).token
                ),
                indices=gateway_pb2.Metrics,
        ): yield b

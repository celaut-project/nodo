import itertools
import mmap
import os
from time import sleep

import grpcbigbuffer as grpcbf
from src.manager.resources_manager import mem_manager

from protos import gateway_pb2_grpc, gateway_pb2, gateway_pb2_grpcbf
from protos import celaut_pb2 as celaut
from protos.buffer_pb2 import Buffer
from protos.gateway_pb2_grpcbf import StartService_input, GetServiceTar_input, \
    GetServiceEstimatedCost_input
from protos import service_capnp

from src.compiler.compile import compile
from src.gateway.utils import save_service, search_definition, \
    generate_gateway_instance, search_file, search_container
from src.gateway.launch_service import launch_service

from src.manager.manager import could_ve_this_sysreq, prune_container, generate_client, get_token_by_uri, spend_gas, \
    container_modify_system_params, get_sysresources, \
    execution_cost, default_initial_cost
from src.manager.metrics import get_metrics
from src.manager.payment_process import validate_payment_process

from src.utils import logger as l
from src.utils.duplicate_grabber import DuplicateGrabber
from src.utils.env import GENERAL_ATTEMPTS, GENERAL_WAIT_TIME, DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH, SHA3_256_ID, \
    MODIFY_SERVICE_SYSTEM_RESOURCES_COST, GAS_COST_FACTOR, REGISTRY, HYCACHE
from src.utils.utils import from_gas_amount, get_only_the_ip_from_context, to_gas_amount, get_network_name, read_file
from src.utils.verify import get_service_hex_main_hash

from src.builder import build


def get_service_buffer_from_registry(service_hash: str) -> bytes:
    return get_from_registry(service_hash=service_hash).value


def get_from_registry(service_hash: str) -> service_capnp.ServiceWithMeta:
    l.LOGGER('Getting ' + service_hash + ' service from the local registry.')
    try:
        file_path: str = REGISTRY + service_hash
        file = open(file_path, 'r+b')
        buf = mmap.mmap(
            file.fileno(),
            length=0,
            access=mmap.ACCESS_READ
        )
        reader = service_capnp.ServiceWithMeta.from_bytes(buf)
        with reader as r:
            return r
        
    except (IOError, FileNotFoundError):
        l.LOGGER('The service was not on registry.')
        raise FileNotFoundError


class Gateway(gateway_pb2_grpc.Gateway):

    def StartService(self, request_iterator, context, **kwargs):
        l.LOGGER('Starting service by ' + str(context.peer()) + ' ...')
        configuration = None
        system_requeriments = None
        initial_gas_amount = None
        max_sysreq = None

        client_id = None
        recursion_guard_token = None

        hashes = []
        parser_generator = grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices=StartService_input,
            partitions_message_mode={1: True, 2: False, 3: True, 4: False, 5: True, 6: True}
        )
        while True:
            try:
                r = next(parser_generator)
            except StopIteration:
                break
            service_hash = None
            service_with_meta = None

            if type(r) is gateway_pb2.Client:
                client_id = r.client_id
                continue

            if type(r) is gateway_pb2.RecursionGuard:
                recursion_guard_token = r.token
                continue

            if type(r) is gateway_pb2.HashWithConfig:
                configuration = r.config
                service_hash = r.hash

                if r.HasField('max_sysreq') and not could_ve_this_sysreq(sysreq=r.max_sysreq):
                    raise Exception("The node can't execute the service with this requeriments.")
                else:
                    max_sysreq = r.max_sysreq

                if r.HasField('min_sysreq'):
                    system_requeriments = r.min_sysreq

                if r.HasField('initial_gas_amount'):
                    initial_gas_amount = from_gas_amount(r.initial_gas_amount)


            # Captura la configuracion si puede.
            elif type(r) is celaut.Configuration:
                configuration = r

            elif type(r) is celaut.Any.Metadata.HashTag.Hash:
                service_hash = r

            # Si me da hash, comprueba que sea sha3-256 y que se encuentre en el registro.
            if service_hash:
                hashes.append(service_hash)
                if configuration and SHA3_256_ID == service_hash.type and \
                        service_hash.value.hex() in [s for s in os.listdir(REGISTRY)]:
                    yield gateway_pb2.buffer__pb2.Buffer(signal=True)
                    try:
                        service_with_meta = get_from_registry(
                            service_hash=service_hash.value.hex()
                        )
                        metadata = celaut.Any.Metadata()
                        metadata.ParseFromString(service_with_meta.metadata)
                        metadata = read_from_capnp(
                            service_with_meta.metadata,
                            celaut.Any.Metadata
                        )
                        if service_hash not in metadata.hashtag.hash:
                            metadata.hashtag.hash.append(service_hash)
                        for b in grpcbf.serialize_to_buffer(
                                indices={},
                                message_iterator=launch_service(
                                    service_buffer=service_with_meta.service,  # TODO check. en este punto pretenderá cargar todo service. Se debe de seguir pasando el puntero a la posicion del disco.
                                    metadata=metadata,
                                    config=configuration,
                                    system_requirements=system_requeriments,
                                    max_sysreq=max_sysreq,
                                    initial_gas_amount=initial_gas_amount,
                                    father_ip=get_only_the_ip_from_context(context_peer=context.peer()),
                                    father_id=client_id,
                                    recursion_guard_token=recursion_guard_token
                                )
                        ): yield b
                        return

                    except Exception as e:
                        l.LOGGER('Exception launching a service ' + str(e))
                        yield gateway_pb2.buffer__pb2.Buffer(signal=True)
                        continue


            elif r is service_capnp.ServiceWithConfig or r is service_capnp.ServiceWithMeta:
                if configuration or r is service_capnp.ServiceWithMeta:
                    value, is_primary = DuplicateGrabber().next(
                        hashes=hashes,
                        generator=parser_generator
                    )
                    if is_primary:
                        parser_generator = itertools.chain([value], parser_generator)
                    else:
                        registry_hash = None
                        for service_hash in hashes:
                            if SHA3_256_ID == service_hash.type:
                                registry_hash = service_hash.value.hex()
                        if registry_hash:
                            for i in range(GENERAL_ATTEMPTS):
                                if registry_hash in [s for s in os.listdir(REGISTRY)]:
                                    try:
                                        service_with_meta = get_from_registry(
                                            service_hash=service_hash.value.hex()
                                        )
                                        metadata = celaut.Any.Metadata()
                                        metadata.ParseFromString(service_with_meta.metadata)
                                        if service_hash not in metadata.hashtag.hash:
                                            metadata.hashtag.hash.append(service_hash)
                                        for b in grpcbf.serialize_to_buffer(
                                                indices={},
                                                message_iterator=launch_service(
                                                    service_buffer=service_with_meta.service,   # TODO check. en este punto pretenderá cargar todo service. Se debe de seguir pasando el puntero a la posicion del disco.
                                                    metadata=metadata,
                                                    config=configuration,
                                                    system_requirements=system_requeriments,
                                                    max_sysreq=max_sysreq,
                                                    initial_gas_amount=initial_gas_amount,
                                                    father_ip=get_only_the_ip_from_context(
                                                        context_peer=context.peer()),
                                                    father_id=client_id,
                                                    recursion_guard_token=recursion_guard_token
                                                )
                                        ): yield b
                                        return

                                    except Exception as e:
                                        l.LOGGER('Exception launching a service ' + str(e))
                                        pass

                                else:
                                    sleep(GENERAL_WAIT_TIME)

                try:
                    r = next(parser_generator)

                    if type(r) not in [service_capnp.ServiceWithConfig, service_capnp.ServiceWithMeta]: raise Exception
                except Exception:
                    raise Exception('Grpcbb error: partition corrupted')

                if type(r) is service_capnp.ServiceWithConfig:
                    configuration = celaut.Configuration(r.config)
                    service_with_meta = r.service  # TODO check

                    if r.HasField('max_sysreq') and not could_ve_this_sysreq(sysreq=r.max_sysreq):
                        raise Exception("The node can't execute the service with this requeriments.")
                    else:
                        max_sysreq = r.max_sysreq

                    if r.HasField('min_sysreq'):
                        system_requeriments = r.min_sysreq

                    if r.HasField('initial_gas_amount'):
                        initial_gas_amount = from_gas_amount(r.initial_gas_amount)
                else:
                    service_with_meta = r

                metadata = celaut.Any.Metadata()
                metadata.ParseFromString(service_with_meta.metadata)
                service_hash = get_service_hex_main_hash(
                    service_buffer=service_with_meta.service,
                    metadata=metadata,
                    other_hashes=hashes
                )

                l.LOGGER('Save service on disk')
                save_service(
                    service_buffer=service_with_meta,
                    service_hash=service_hash if service_hash else None
                )
                if configuration:
                    l.LOGGER('Launch service with configuration')
                    for buffer in grpcbf.serialize_to_buffer(
                            indices={},
                            message_iterator=launch_service(
                                service_buffer=service_with_meta.service,
                                metadata=metadata,
                                config=configuration,
                                system_requirements=system_requeriments,
                                max_sysreq=max_sysreq,
                                initial_gas_amount=initial_gas_amount,
                                service_id=service_hash,
                                father_ip=get_only_the_ip_from_context(context_peer=context.peer()),
                                father_id=client_id,
                                recursion_guard_token=recursion_guard_token
                            )
                    ): yield buffer
                    return

        l.LOGGER('The service is not in the registry and the request does not have the definition.' \
                 + str([(hash.type.hex(), hash.value.hex()) for hash in hashes]))

        try:
            for b in grpcbf.serialize_to_buffer(
                    message_iterator=launch_service(
                        service_buffer=search_definition(
                                hashes=hashes
                            ),
                        metadata=celaut.Any.Metadata(
                            hashtag=celaut.Any.Metadata.HashTag(
                                hash=hashes
                            )
                        ),
                        config=configuration,
                        system_requirements=system_requeriments,
                        max_sysreq=max_sysreq,
                        initial_gas_amount=initial_gas_amount,
                        father_ip=get_only_the_ip_from_context(context_peer=context.peer()),
                        father_id=client_id,
                        recursion_guard_token=recursion_guard_token
                    )
            ): yield b

        except Exception as e:
            raise Exception('Was imposible start the service. ' + str(e))

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

    def GetFile(self, request_iterator, context, **kwargs):
        l.LOGGER('Request for give a service definition.')
        hashes = []
        for hash in grpcbf.parse_from_buffer(
                request_iterator=request_iterator,
                indices=celaut.Any.Metadata.HashTag.Hash,
                partitions_message_mode=True
        ):
            try:
                # Comprueba que sea sha256 y que se encuentre en el registro.
                hashes.append(hash)
                if SHA3_256_ID == hash.type and \
                        hash.value.hex() in [s for s in os.listdir(REGISTRY)]:
                    yield gateway_pb2.buffer__pb2.Buffer(signal=True)  # Say stop to send more hashes.
                    any = celaut.Any()
                    any.ParseFromString(
                        get_from_registry(
                            service_hash=hash.value.hex()
                        )
                    )
                    for b in grpcbf.serialize_to_buffer(  # TODO check.
                            message_iterator=(
                                    celaut.Any,
                                    any,
                                    grpcbf.Dir(REGISTRY + '/' + hash.value.hex() + '/p2')
                            ),
                            partitions_model=StartService_input_partitions_v2[2]
                    ): yield b
            except:
                pass

        try:
            for b in grpcbf.serialize_to_buffer(
                    message_iterator=next(search_file(
                        ignore_network=get_network_name(
                            ip_or_uri=get_only_the_ip_from_context(context_peer=context.peer())
                        ),
                        hashes=hashes
                    ))
                    # It's not verifying the content, because we couldn't 've the format for prune metadata in it. The final client will've to check it.
            ): yield b

        except:
            raise Exception('Was imposible get the service definition.')

    def Compile(self, request_iterator, context, **kwargs):
        l.LOGGER('Go to compile a proyect.')
        input = grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices=gateway_pb2.CompileInput,
            partitions_model=[Buffer.Head.Partition(index={1: Buffer.Head.Partition()}),
                              Buffer.Head.Partition(index={2: Buffer.Head.Partition()})],  # Deprecated usage of partitions for compile, not needed with capnp and mmap readers.
            partitions_message_mode=[False, True]
        )
        if next(input) != gateway_pb2.CompileInput: raise Exception('Compile Input wrong.')
        for b in compile(
                repo=next(input)
        ): yield b

    def GetServiceTar(self, request_iterator, context, **kwargs):
        # TODO se debe de hacer que gestione mejor tomar todo el servicio, como hace GetServiceEstimatedCost.

        service_hash = None
        service_buffer = None
        l.LOGGER('Request for give a service container.')
        for r in grpcbf.parse_from_buffer(
                request_iterator=request_iterator,
                indices=GetServiceTar_input,
                partitions_message_mode=True
        ):

            # Si me da hash, comprueba que sea sha256 y que se encuentre en el registro.
            if type(r) is celaut.Any.Metadata.HashTag.Hash and SHA3_256_ID == r.type:
                service_hash = r.value.hex()
                break

            # Si me da servicio.
            if type(r) is celaut.Any:
                service_hash = get_service_hex_main_hash(service_buffer=r.value)
                service_partitions_iterator = grpcbf.parse_from_buffer.conversor(
                    iterator=itertools.chain([r.value]),
                    pf_object=gateway_pb2.ServiceWithMeta,
                    remote_partitions_model=gateway_pb2_grpcbf.StartService_input_partitions_v2[2],
                    mem_manager=mem_manager,
                    yield_remote_partition_dir=False,
                    partitions_message_mode=[True, False]
                )
                save_service(
                    service_buffer=service_buffer
                )
                service_buffer = r.value
                break

        l.LOGGER('Getting the container of service ' + service_hash)
        if service_hash and service_hash in [s for s in os.listdir(REGISTRY)]:
            try:
                os.system('docker save ' + service_hash + '.service > ' + HYCACHE + service_hash + '.tar')
                l.LOGGER('Returned the tar container buffer.')
                yield grpcbf.get_file_chunks(filename=HYCACHE + service_hash + '.tar')
            except:
                l.LOGGER('Error saving the container ' + service_hash)
        elif service_buffer:
            # Puede buscar el contenedor en otra red distinta a la del solicitante.
            try:
                yield search_container(
                    ignore_network=get_network_name(
                        ip_or_uri=get_only_the_ip_from_context(context_peer=context.peer())
                    ),
                    service_buffer=service_buffer,
                    metadata=celaut.Any.Metadata()
                )
            except:
                l.LOGGER('The service ' + service_hash + ' was not found.')

        yield gateway_pb2.buffer__pb2.Buffer(separator=True)
        raise Exception('Was imposible get the service container.')

    # Estimacion de coste de ejecución de un servicio con la cantidad de gas por defecto.
    def GetServiceEstimatedCost(self, request_iterator, context, **kwargs):
        # TODO check cost in other peers (use RecursionGuard to prevent infinite loops).

        l.LOGGER('Request for the cost of a service.')
        parse_iterator = grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices=GetServiceEstimatedCost_input,
            partitions_model=StartService_input_partitions_v2,
            partitions_message_mode={1: True, 2: [True, False], 3: True, 4: [True, False], 5: True, 6: True}
        )

        client_id = None
        recursion_guard_token = None
        initial_service_cost = None
        cost = None
        initial_service_cost = None
        service_hash = None
        second_partition_dir = None
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
                r = service_hash = r.hash

            if type(r) is celaut.Any.Metadata.HashTag.Hash and SHA3_256_ID == r.type:
                if r.value.hex() in [s for s in os.listdir(REGISTRY)]:
                    yield gateway_pb2.buffer__pb2.Buffer(signal=True)
                    try:
                        p1 = get_from_registry(
                            service_hash=r.value.hex()
                        )
                        cost = execution_cost(
                            service_buffer=p1.value,
                            metadata=p1.metadata
                        ) * GAS_COST_FACTOR
                        break
                    except build.UnsupportedArquitectureException as e:
                        raise e
                    except Exception as e:
                        yield gateway_pb2.buffer__pb2.Buffer(signal=True)
                        continue
                elif DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH:
                    raise Exception("I dont've the service.")

            if r is gateway_pb2.ServiceWithMeta:
                if DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH: raise Exception("I dont've the service.")
                value, is_primary = DuplicateGrabber().next(
                    hashes=[service_hash],
                    generator=parse_iterator
                )
                service_with_meta = value
                if is_primary:
                    second_partition_dir = next(parse_iterator)
                elif service_hash.type == SHA3_256_ID:
                    for i in range(GENERAL_ATTEMPTS):
                        if service_hash.value.hex() in [s for s in os.listdir(REGISTRY)]:
                            second_partition_dir = get_from_registry(
                                service_hash=r.value.hex()
                            )
                        else:
                            sleep(GENERAL_WAIT_TIME)
                if not second_partition_dir:
                    next(parse_iterator)  # Download all the message again.
                    second_partition_dir = next(parse_iterator)

                if type(second_partition_dir) is not str: raise Exception('Error: fail sending service.')
                try:
                    cost = execution_cost(
                        service_buffer=get_service_buffer_from_registry(
                            service_hash=save_service(
                                service_buffer=service_with_meta,
                                service_hash=service_hash
                            )
                        ),
                        metadata=service_with_meta.metadata
                    ) * GAS_COST_FACTOR
                except build.UnsupportedArquitectureException as e:
                    raise e
                break

            if r is gateway_pb2.ServiceWithConfig:
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
                                service_buffer=service_with_meta,
                                service_hash=service_hash
                            )
                        ),
                        metadata=service_with_meta.metadata
                    ) * GAS_COST_FACTOR
                except build.UnsupportedArquitectureException as e:
                    raise e
                break

        if not initial_service_cost:
            initial_service_cost: int = default_initial_cost(
                father_id=client_id if client_id else get_only_the_ip_from_context(context_peer=context.peer())
            )
        cost: int = cost + initial_service_cost if cost else initial_service_cost
        l.LOGGER('Execution cost for a service is requested, cost -> ' + str(cost) + ' with benefit ' + str(0))
        if cost is None: raise Exception("I dont've the service.")
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
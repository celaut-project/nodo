from typing import Generator
from buffer_pb2 import Buffer

import celaut_pb2 as celaut
import build, utils
from manager import COMPUTE_POWER_RATE, COST_OF_BUILD, DEFAULT_SYSTEM_RESOURCES, EXECUTION_BENEFIT, MANAGER_ITERATION_TIME, \
    add_container, add_peer, container_modify_system_params, default_cost, could_ve_this_sysreq, execution_cost, get_metrics, get_sysresources, manager_thread, prune_container, set_external_on_cache, \
    spend_gas, start_service_cost, validate_payment_process, COST_AVERAGE_VARIATION, GAS_COST_FACTOR, MODIFY_SERVICE_SYSTEM_RESOURCES_COST, get_token_by_uri
from compile import REGISTRY, HYCACHE, compile
import logger as l
from verify import SHA3_256_ID, check_service, get_service_hex_main_hash, completeness
import subprocess, os, threading, shutil
import grpc, gateway_pb2, gateway_pb2_grpc
from concurrent import futures
import docker as docker_lib
import netifaces as ni
from gateway_pb2_grpcbf import StartService_input, GetServiceEstimatedCost_input, GetServiceTar_input, StartService_input_partitions_v2
import grpcbigbuffer as grpcbf
import iobigdata as iobd
from manager import insert_instance_on_mongo, DOCKER_NETWORK, LOCAL_NETWORK, set_external_on_cache
from contracts.main.utils import get_ledger_and_contract_addr_from_contract
from logger import GET_ENV

DOCKER_CLIENT = lambda: docker_lib.from_env()
GATEWAY_PORT = GET_ENV(env = 'GATEWAY_PORT', default = 8090)
MEMORY_LOGS = GET_ENV(env = 'MEMORY_LOGS', default = False)
IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER = GET_ENV(env = 'IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER', default = True)
SEND_ONLY_HASHES_ASKING_COST = GET_ENV(env = 'SEND_ONLY_HASHES_ASKING_COST', default=False)
DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH = GET_ENV(env = 'DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH', default=False)


#  TODO auxiliares
DEFAULT_PROVISIONAL_CONTRACT = open('contracts/vyper_gas_deposit_contract/bytecode', 'rb').read()
from contracts.vyper_gas_deposit_contract.interface import CONTRACT_HASH as DEFAULT_PROVISIONAL_CONTRACT_HASH

def generate_contract_ledger() -> celaut.Service.Api.ContractLedger:  # TODO generate_contract_ledger tambien es un método auxiliar.
    contract_ledger = celaut.Service.Api.ContractLedger()
    contract_ledger.contract = DEFAULT_PROVISIONAL_CONTRACT
    contract_ledger.ledger, contract_ledger.contract_addr = get_ledger_and_contract_addr_from_contract(DEFAULT_PROVISIONAL_CONTRACT_HASH)[0].values()
    return contract_ledger

def generate_gateway_instance(network: str, peer_id: str = '') -> gateway_pb2.Instance:
    instance = celaut.Instance()

    uri = celaut.Instance.Uri()
    try:
        uri.ip = ni.ifaddresses(network)[ni.AF_INET][0]['addr']
    except ValueError as e:
        l.LOGGER('You must specify a valid interface name ' + network)
        raise Exception('Error generating gateway instance --> ' + str(e))
    uri.port = GATEWAY_PORT
    uri_slot = celaut.Instance.Uri_Slot()
    uri_slot.internal_port = GATEWAY_PORT
    uri_slot.uri.append(uri)
    instance.uri_slot.append(uri_slot)
    
    slot = celaut.Service.Api.Slot()
    slot.port = GATEWAY_PORT
    instance.api.slot.append(slot)

    instance.api.contract_ledger.append(generate_contract_ledger())
    return gateway_pb2.Instance(
        instance = instance,
        token = peer_id
    )


def set_config(container_id: str, config: celaut.Configuration, resources: celaut.Sysresources, api: celaut.Service.Container.Config):
    __config__ = celaut.ConfigurationFile()
    __config__.gateway.CopyFrom(generate_gateway_instance(network=DOCKER_NETWORK).instance)
    if config: __config__.config.CopyFrom(config)
    if resources: __config__.initial_sysresources.CopyFrom(resources)

    os.mkdir(HYCACHE + container_id)
    # TODO: Check if api.format is valid or make the serializer for it.

    with open(HYCACHE + container_id + '/__config__', 'wb') as file:
        file.write(__config__.SerializeToString())
    while 1:
        try:
            subprocess.run(
                '/usr/bin/docker cp ' + HYCACHE + container_id + '/__config__ ' + container_id + ':/'+'/'.join(api.path),
                shell=True
            )
            break
        except subprocess.CalledProcessError as e:
            l.LOGGER(e.output)
    os.remove(HYCACHE + container_id + '/__config__')
    os.rmdir(HYCACHE + container_id)


def create_container(id: str, entrypoint: list, use_other_ports=None) -> docker_lib.models.containers.Container:
    try:
        return DOCKER_CLIENT().containers.create(
            image = id + '.docker', # https://github.com/moby/moby/issues/20972#issuecomment-193381422
            entrypoint = ' '.join(entrypoint),
            ports = use_other_ports
        )
    except docker_lib.errors.ImageNotFound:
        l.LOGGER('IMAGE WOULD BE IN DOCKER REGISTRY. BUT NOT FOUND.')     # LOS ERRORES DEBERIAN LANZAR ALGUN TIPO DE EXCEPCION QUE LLEGUE HASTA EL GRPC.
    except docker_lib.errors.APIError:
        l.LOGGER('DOCKER API ERROR ')


def service_balancer(
    service_buffer: bytes, 
    metadata: celaut.Any.Metadata, 
    ignore_network: str = None,
    initial_gas_amount: int = None,
    ) -> dict: # sorted by cost, dict of celaut.Instances or 'local'  and cost.
    class PeerCostList:
        # Sorts the list from the element with the smallest weight to the element with the largest weight.
        
        def __init__(self) -> None:
            self.dict = {} # elem : weight
        
        def add_elem(self, weight: int, elem: str = 'local' ) -> None:
            self.dict.update({elem: weight})
        
        def get(self) -> dict:
            return {k : v for k, v in sorted(self.dict.items(), key=lambda item: item[1].cost)}  # TODO ordenar en function de la varianza tambien.


    peers: PeerCostList = PeerCostList()
    # TODO If there is noting on meta. Need to check the architecture on the buffer and write it on metadata.
    try:
        peers.add_elem(
            weight = gateway_pb2.EstimatedCost(
                cost = utils.to_gas_amount(execution_cost(service_buffer = service_buffer, metadata = metadata) * GAS_COST_FACTOR + (initial_gas_amount if initial_gas_amount else int(default_cost()))), 
                variance = 0
            )
        )
    except build.UnsupportedArquitectureException:
        l.LOGGER('UNSUPPORTED ARQUITECTURE.')
        pass
    except Exception as e:
        l.LOGGER('Error getting the local cost ' + str(e))
        raise e

    try:
        for peer in utils.peers_uri_iterator(ignore_network = ignore_network):
            l.LOGGER('Check cost on peer ' + peer['ip'] + ' ' + str(peer['port']))
            # TODO could use async or concurrency ¿numba?. And use timeout.
            peer_uri: str = peer['ip']+':'+str(peer['port'])
            try:
                peers.add_elem(
                    elem = peer_uri,
                    weight = utils.from_gas_amount(next(grpcbf.client_grpc(
                        method =  gateway_pb2_grpc.GatewayStub(
                                    grpc.insecure_channel(
                                        peer_uri
                                    )
                                ).GetServiceEstimatedCost,
                        indices_parser = gateway_pb2.EstimatedCost,
                        partitions_message_mode_parser = True,
                        indices_serializer = GetServiceEstimatedCost_input,
                        partitions_serializer = {2: StartService_input_partitions_v2[2]},
                        input = utils.service_extended(service_buffer = service_buffer, metadata = metadata, send_only_hashes = SEND_ONLY_HASHES_ASKING_COST),  # TODO añadir initial_gas_amount y el resto de la configuracion inicial, si es que se especifica.
                    ))
                ))
            except Exception as e: l.LOGGER('Error taking the cost on '+ peer_uri +' : '+str(e))
    except Exception as e: l.LOGGER('Error iterating peers on service balancer ->>'+ str(e))

    try:
        return peers.get()
    except Exception as e:
        l.LOGGER('Error during balancer, ' + str(e))
        return {}


def launch_service(
        service_buffer: bytes, 
        metadata: celaut.Any.Metadata, 
        father_ip: str, 
        id: str = None,
        system_requeriments: celaut.Sysresources = None,
        max_sysreq = None,
        config: celaut.Configuration = None,
        initial_gas_amount: int = None,
    ) -> gateway_pb2.Instance:
    l.LOGGER('Go to launch a service. ')
    if service_buffer == None: raise Exception("Service object can't be None")
    getting_container = False  # Here it asks the balancer if it should assign the job to a peer.
    is_complete = completeness(
                            service_buffer = service_buffer,
                            metadata = metadata,
                            id = id,
                        )
    while True:
        abort_it = True
        for peer_instance_uri, cost in service_balancer(
            service_buffer = service_buffer,
            metadata = metadata,
            ignore_network = utils.get_network_name(
                ip_or_uri = father_ip
            ) if IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER else None,
            initial_gas_amount  = initial_gas_amount
        ).items():
            if abort_it: abort_it = False
            l.LOGGER('Balancer select peer ' + str(peer_instance_uri) + ' with cost ' + str(cost))
            
            # Delegate the service instance execution.
            if peer_instance_uri != 'local':
                try:
                    l.LOGGER('El servicio se lanza en el nodo con uri ' + str(peer_instance_uri))
                    refound_gas = []
                    if not spend_gas(
                        token_or_container_ip = father_ip,
                        gas_to_spend =  cost.cost * GAS_COST_FACTOR * cost.variance * COST_AVERAGE_VARIATION,
                        refund_gas_function_container = refound_gas
                    ): raise Exception('Launch service error spending gas for '+father_ip)
                    service_instance = next(grpcbf.client_grpc(
                        method = gateway_pb2_grpc.GatewayStub(
                                    grpc.insecure_channel(
                                        peer_instance_uri
                                    )
                                ).StartService, # TODO se debe hacer que al pedir un servicio exista un timeout.
                        partitions_message_mode_parser = True,
                        indices_serializer = StartService_input,
                        partitions_serializer = StartService_input_partitions_v2,
                        indices_parser = gateway_pb2.Instance,
                        input = utils.service_extended(
                                service_buffer = service_buffer,
                                metadata = metadata,
                                config = config,
                                min_sysreq = system_requeriments,
                                max_sysreq = max_sysreq
                            )
                    ))
                    set_external_on_cache(
                        father_ip = father_ip,
                        ip_or_uri =  peer_instance_uri, # Add node_uri.
                        external_token = service_instance.token  # Add token.
                    )
                    service_instance.token = father_ip + '##' + peer_instance_uri.split(':')[0] + '##' + service_instance.token  # TODO adapt for ipv6 too.
                    return service_instance
                except Exception as e:
                    l.LOGGER('Failed starting a service on peer, occurs the eror: ' + str(e))
                    try:
                        refound_gas.pop()()
                    except IndexError: pass

            #  The node launches the service locally.
            if getting_container: l.LOGGER('El nodo lanza el servicio localmente.')
            if not system_requeriments: system_requeriments = DEFAULT_SYSTEM_RESOURCES
            refound_gas = []
            if not spend_gas(
                token_or_container_ip = father_ip,
                gas_to_spend = start_service_cost(
                    initial_gas_amount = initial_gas_amount if initial_gas_amount else default_cost(),
                    service_buffer = service_buffer,
                    metadata = metadata
                ) * GAS_COST_FACTOR,
            ): raise Exception('Launch service error spending gas for '+father_ip)
            try:
                id = build.build(
                        service_buffer = service_buffer, 
                        metadata = metadata,
                        id = id,
                        get_it = not getting_container,
                        complete = is_complete
                    )  #  If the container is not built, build it.
            except build.UnsupportedArquitectureException as e: 
                try:
                    refound_gas.pop()()
                except IndexError: pass
                raise e
            except build.WaitBuildException:
                # If it does not have the container, it takes it from another node in the background and requests
                #  the instance from another node as well.
                try:
                    refound_gas.pop()() # Refund the gas.
                except IndexError: pass
                getting_container = True
                continue
            except Exception as e:
                try:
                    refound_gas.pop()() # Refund the gas.
                except IndexError: pass
                l.LOGGER(str(e))
                raise e

            # Now serialize the part of the service that is needed.
            service = celaut.Service()
            service.ParseFromString(service_buffer)
            # If the request is made by a local service.
            if utils.get_network_name(father_ip) == DOCKER_NETWORK:
                container = create_container(
                    id = id,
                    entrypoint = service.container.entrypoint
                )

                set_config(container_id = container.id, config = config, resources = system_requeriments, api = service.container.config)

                # The container must be started after adding the configuration file and
                #  before requiring its IP address, since docker assigns it at startup.

                try:
                    container.start()
                except docker_lib.errors.APIError as e:
                    l.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' '+str(e)) # TODO LOS ERRORES DEBERIAN LANZAR ALGUN TIPO DE EXCEPCION QUE LLEGUE HASTA EL GRPC.

                # Reload this object from the server again and update attrs with the new data.
                container.reload()

                for slot in service.api.slot:
                    uri_slot = celaut.Instance.Uri_Slot()
                    uri_slot.internal_port = slot.port

                    # Since it is internal, we know that it will only have one possible address per slot.
                    uri = celaut.Instance.Uri()
                    uri.ip = container.attrs['NetworkSettings']['IPAddress']
                    uri.port = slot.port
                    uri_slot.uri.append(uri)

            # Si hace la peticion un servicio de otro nodo.
            else:
                assigment_ports = {slot.port: utils.get_free_port() for slot in service.api.slot}
                container = create_container(
                    use_other_ports = assigment_ports,
                    id = id,
                    entrypoint = service.container.entrypoint
                )
                set_config(container_id = container.id, config = config, resources = system_requeriments, api = service.container.config)
                try:
                    container.start()
                except docker_lib.errors.APIError as e:
                    # TODO LOS ERRORES DEBERÍAN LANZAR UNA EXCEPCION QUE LLEGUE HASTA EL GRPC.
                    l.LOGGER('ERROR ON CONTAINER '+ str(container.id) + ' '+str(e)) 

                # Reload this object from the server again and update attrs with the new data.
                container.reload()

                for port in assigment_ports:
                    uri_slot = celaut.Instance.Uri_Slot()
                    uri_slot.internal_port = port

                    # for host_ip in host_ip_list:
                    uri = celaut.Instance.Uri()
                    uri.ip = utils.get_local_ip_from_network(
                        network = utils.get_network_name(ip_or_uri=father_ip)
                    )
                    uri.port = assigment_ports[port]
                    uri_slot.uri.append(uri)

            l.LOGGER('Thrown out a new instance by ' + father_ip + ' of the container_id ' + container.id)
            return gateway_pb2.Instance(
                token = add_container(
                            father_ip = father_ip,
                            container = container,
                            initial_gas_amount = initial_gas_amount if initial_gas_amount else default_cost(father_ip = father_ip),
                            system_requeriments_range = gateway_pb2.ModifyServiceSystemResourcesInput(min_sysreq = system_requeriments, max_sysreq = system_requeriments)
                        ),
                instance = celaut.Instance(
                        api = service.api,
                        uri_slot = [uri_slot]
                    )
            )

        if abort_it: 
            l.LOGGER("Can't launch this service "+id)
            raise Exception("Can't launch this service "+id)

def save_service(
    service_p1: bytes, 
    service_p2: str,
    metadata: celaut.Any.Metadata, 
    hash: str = None
    ) -> str:
    # If the service is not on the registry, save it.
    if not hash: hash = get_service_hex_main_hash(
        service_buffer = (service_p1, service_p2) if service_p2 else None, 
        metadata = metadata
        )
    if not os.path.isdir(REGISTRY+hash):
        os.mkdir(REGISTRY+hash)
        with open(REGISTRY + hash + '/p1', 'wb') as file: # , iobd.mem_manager(len=len(service_p1)): TODO check mem-58 bug.
            file.write(
                celaut.Any(
                    metadata = metadata,
                    value = service_p1
                ).SerializeToString()
            )
        if service_p2:
            shutil.move(service_p2, REGISTRY+hash+'/p2')
    return hash

def search_container(
        service_buffer: bytes, 
        metadata: celaut.Any.Metadata,
        ignore_network: str = None
    ) -> Generator[gateway_pb2.buffer__pb2.Buffer, None, None]:
    # Search a service tar container.
    for peer in utils.peers_uri_iterator(ignore_network = ignore_network):
        try:
            next(grpcbf.client_grpc(
                method = gateway_pb2_grpc.GatewayStub(
                            grpc.insecure_channel(peer['ip'] + ':' + str(peer['port']))
                        ).GetServiceTar,
                input = utils.service_extended(
                            service_buffer = service_buffer,
                            metadata = metadata
                        ),
                indices_serializer = GetServiceTar_input
            ))
            break
        except: pass

def search_file(hashes: list, ignore_network: str = None) -> Generator[celaut.Any, None, None]:
    # TODO: It can search for other 'Service ledger' or 'ANY ledger' instances that could've this type of files.
    for peer in  utils.peers_uri_iterator(ignore_network = ignore_network):
        try:
            for buffer in grpcbf.client_grpc(
                method = gateway_pb2_grpc.GatewayStub(
                            grpc.insecure_channel(peer['ip'] + ':' + str(peer['port']))
                        ).GetFile,
                output_field = celaut.Any,
                input = utils.service_hashes(
                            hashes = hashes
                        )
            ): yield buffer
        except: pass

def search_definition(hashes: list, ignore_network: str = None) -> bytes:
    #  Search a service description.
    for any in  search_file(
        hashes = hashes,
        ignore_network = ignore_network
    ):
        if check_service(
                service_buffer = any.value,
                hashes = hashes
            ):
            #  Save the service on the registry.
            save_service(  # TODO
                service_p1 = any.value,
                metadata = any.metadata
            )
            return any.value

    l.LOGGER('The service '+ hashes[0].value.hex() + ' was not found.')
    raise Exception('The service ' + hashes[0].value.hex() + ' was not found.')

def get_service_buffer_from_registry(hash: str) -> bytes:
    return get_from_registry(hash = hash).value

def get_from_registry(hash: str) -> celaut.Any:
    l.LOGGER('Getting ' + hash + ' service from the local registry.')
    first_partition_dir = REGISTRY + hash + '/p1'
    try:
        with iobd.mem_manager(2*os.path.getsize(first_partition_dir)) as iolock:
            any = celaut.Any()
            any.ParseFromString(utils.read_file(filename = first_partition_dir))
            return any
    except (IOError, FileNotFoundError):
        l.LOGGER('The service was not on registry.')
        raise FileNotFoundError


class Gateway(gateway_pb2_grpc.Gateway):

    def StartService(self, request_iterator, context):
        l.LOGGER('Starting service ...')
        configuration = None
        system_requeriments = None
        initial_gas_amount = None
        max_sysreq = None
        hashes = []
        parser_generator = grpcbf.parse_from_buffer(
            request_iterator = request_iterator, 
            indices = StartService_input,
            partitions_model = StartService_input_partitions_v2,
            partitions_message_mode = {1: True, 2: [True, False], 3: True, 4: [True, False]}
        )
        while True:
            try:
                r = next(parser_generator)
            except StopIteration: break
            hash = None
            service_with_meta = None

            if type(r) is gateway_pb2.HashWithConfig:
                configuration = r.config
                hash = r.hash
                
                if r.HasField('max_sysreq') and not could_ve_this_sysreq(sysreq = r.max_sysreq): 
                    raise Exception("The node can't execute the service with this requeriments.")
                else: max_sysreq = r.max_sysreq
                
                if r.HasField('min_sysreq'):
                    system_requeriments = r.min_sysreq

                if r.HasField('initial_gas_amount'):
                    initial_gas_amount = utils.from_gas_amount(r.initial_gas_amount)


            # Captura la configuracion si puede.
            elif type(r) is celaut.Configuration:
                configuration = r
            
            elif type(r) is celaut.Any.Metadata.HashTag.Hash:
                hash = r

            # Si me da hash, comprueba que sea sha3-256 y que se encuentre en el registro.
            if hash:
                hashes.append(hash)
                if configuration and SHA3_256_ID == hash.type and \
                    hash.value.hex() in [s for s in os.listdir(REGISTRY)]:
                    yield gateway_pb2.buffer__pb2.Buffer(signal = True)
                    try:
                        p1 = get_from_registry(
                                    hash = hash.value.hex()
                                )
                        if hash not in p1.metadata.hashtag.hash:
                            p1.metadata.hashtag.hash.append(hash)
                        for b in grpcbf.serialize_to_buffer(
                            indices={},
                            message_iterator = launch_service(
                                service_buffer = p1.value,
                                metadata = p1.metadata, 
                                config = configuration,
                                system_requeriments = system_requeriments,
                                max_sysreq = max_sysreq,
                                initial_gas_amount = initial_gas_amount if initial_gas_amount else default_cost(father_ip = utils.get_only_the_ip_from_context(context_peer = context.peer())),  # TODO : modificar context.peer() por el id que se use.
                                father_ip = utils.get_only_the_ip_from_context(context_peer = context.peer())
                            )
                        ): yield b
                        return

                    except Exception as e:
                        l.LOGGER('Exception launching a service ' + str(e))
                        yield gateway_pb2.buffer__pb2.Buffer(signal = True)
                        continue
            

            elif r is gateway_pb2.ServiceWithConfig: # We now that is partitionated.
                try:
                    r = next(parser_generator)
                    if type(r) is not gateway_pb2.ServiceWithConfig: raise Exception
                except Exception: raise Exception('Grpcbf error: partition corrupted')
                configuration = r.config
                service_with_meta = r.service

                if r.HasField('max_sysreq') and not could_ve_this_sysreq(sysreq = r.max_sysreq): 
                    raise Exception("The node can't execute the service with this requeriments.")
                else: max_sysreq = r.max_sysreq
                
                if r.HasField('min_sysreq'):
                    system_requeriments = r.min_sysreq

                if r.HasField('initial_gas_amount'):
                    initial_gas_amount = utils.from_gas_amount(r.initial_gas_amount)


            elif r is gateway_pb2.ServiceWithMeta:
                try:
                    r = next(parser_generator) # Can raise StopIteration
                    if type(r) is not gateway_pb2.ServiceWithMeta: raise Exception
                except Exception: raise Exception('Grpcbf error: partition corrupted')
                service_with_meta = r
 
            # Si me da servicio.  
            if service_with_meta:
                # Iterate the second partition.
                try:
                    second_partition_dir = next(parser_generator)
                    if type(second_partition_dir) is not str: raise Exception
                except: raise Exception('Grpcbf error: partition corrupted')
                hash = get_service_hex_main_hash(
                    service_buffer = (service_with_meta.service, second_partition_dir) if second_partition_dir else service_with_meta.service,
                    metadata = service_with_meta.metadata,
                    other_hashes = hashes
                    )

                save_service(
                    service_p1 = service_with_meta.service.SerializeToString(),
                    service_p2 = second_partition_dir,
                    metadata = service_with_meta.metadata,
                    hash = hash if hash else None
                )
                if configuration:
                    for buffer in grpcbf.serialize_to_buffer(
                        indices={},
                        message_iterator = launch_service(
                            service_buffer = service_with_meta.service.SerializeToString(),
                            metadata = service_with_meta.metadata, 
                            config = configuration,
                            system_requeriments = system_requeriments,
                            max_sysreq = max_sysreq,
                            initial_gas_amount = initial_gas_amount if initial_gas_amount else default_cost(father_ip = utils.get_only_the_ip_from_context(context_peer = context.peer())),  # TODO : modificar context.peer() por el id que se use.
                            id = hash,
                            father_ip = utils.get_only_the_ip_from_context(context_peer = context.peer())
                        )
                    ): yield buffer
                    return

                
        l.LOGGER('The service is not in the registry and the request does not have the definition.' \
            + str([(hash.type.hex(), hash.value.hex()) for hash in hashes]))
        
        try:
            for b in grpcbf.serialize_to_buffer(
                message_iterator = launch_service(
                    indices={},
                    service_buffer = search_definition(hashes = hashes),
                    metadata = celaut.Any.Metadata(
                        hashtag = celaut.Any.Metadata.HashTag(
                            hash = hashes
                        )
                    ), 
                    config = configuration,
                    system_requeriments = system_requeriments,
                    max_sysreq = max_sysreq,
                    initial_gas_amount = initial_gas_amount if initial_gas_amount else default_cost(father_ip = utils.get_only_the_ip_from_context(context_peer = context.peer())),  # TODO : modificar context.peer() por el id que se use.
                    father_ip = utils.get_only_the_ip_from_context(context_peer = context.peer())
                )
            ): yield b

        except Exception as e:
            raise Exception('Was imposible start the service. ' + str(e))


    def StopService(self, request_iterator, context):
        try:
            l.LOGGER('Stopping service.')
            for b in grpcbf.serialize_to_buffer(
                message_iterator = gateway_pb2.Refund(
                    amount = utils.to_gas_amount(prune_container(
                        token = next(grpcbf.parse_from_buffer(
                                    request_iterator = request_iterator,
                                    indices = gateway_pb2.TokenMessage,
                                    partitions_message_mode=True
                                )).token
                    ))
                )
            ): yield b
        except Exception as e:
            raise Exception('Was imposible stop the service. ' + str(e))


    
    def Hynode(self, request_iterator, context):
        instance = next(grpcbf.parse_from_buffer(
            request_iterator = request_iterator,
            indices = gateway_pb2.Instance,
            partitions_message_mode = True
        ))
        l.LOGGER('\nAdding peer ' + str(instance))
        peer_id = instance.instance.uri_slot[0].uri[0].ip   # TODO use generic id for peers.
        insert_instance_on_mongo(instance = instance)
        if not add_peer(
            peer_id = peer_id  
        ): raise Exception('Was imposible add the peer.')

        for b in grpcbf.serialize_to_buffer(
            generate_gateway_instance(
                network = utils.get_network_name(
                    ip_or_uri = utils.get_only_the_ip_from_context(
                        context_peer = context.peer()
                    )
                ),
                peer_id = peer_id
            )            
        ): yield b


    def ModifyServiceSystemResources(self, request_iterator, context):
        l.LOGGER('Request for modify service system resources.')
        token = get_token_by_uri(
                uri = utils.get_only_the_ip_from_context(context_peer = context.peer())
            )
        refound_gas = []
        if not spend_gas(
            token_or_container_ip = token, 
            gas_to_spend = MODIFY_SERVICE_SYSTEM_RESOURCES_COST * GAS_COST_FACTOR,
            refund_gas_function_container = refound_gas
        ): raise Exception('Launch service error spending gas for '+context.peer())
        if not container_modify_system_params(
            token = token,
            system_requeriments_range = next(grpcbf.parse_from_buffer(
                request_iterator = request_iterator,
                indices = gateway_pb2.ModifyServiceSystemResourcesInput,
                partitions_message_mode = True
            ))
        ): 
            try:
                refound_gas.pop()()
            except IndexError: pass
            raise Exception('Exception on service modify method.')

        for b in grpcbf.serialize_to_buffer(
            message_iterator = get_sysresources(
                token = token
            )
        ): yield b

    def GetFile(self, request_iterator, context):
        l.LOGGER('Request for give a service definition.')
        hashes = []
        for hash in grpcbf.parse_from_buffer(
            request_iterator = request_iterator, 
            indices = celaut.Any.Metadata.HashTag.Hash,
            partitions_message_mode=True
            ):
            try:
                # Comprueba que sea sha256 y que se encuentre en el registro.
                hashes.append(hash)
                if SHA3_256_ID == hash.type and \
                    hash.value.hex() in [s for s in os.listdir(REGISTRY)]:
                    yield gateway_pb2.buffer__pb2.Buffer(signal = True) # Say stop to send more hashes.
                    any = celaut.Any()
                    any.ParseFromString(
                        get_from_registry(
                                hash = hash.value.hex()
                            )
                    )
                    for b in grpcbf.serialize_to_buffer( # TODO check.
                        message_iterator = (
                            celaut.Any,
                            any,
                            grpcbf.Dir(REGISTRY+'/'+hash.value.hex()+'/p2')
                        ),
                        partitions_model = StartService_input_partitions_v2[2]
                    ): yield b
            except: pass
        
        try:
            for b in  grpcbf.serialize_to_buffer(
                message_iterator = next(search_file(
                    ignore_network = utils.get_network_name(
                            ip_or_uri = utils.get_only_the_ip_from_context(context_peer = context.peer())
                        ),
                    hashes = hashes
                )) # It's not verifying the content, because we couldn't 've the format for prune metadata in it. The final client will've to check it.                
            ): yield b

        except:
            raise Exception('Was imposible get the service definition.')

    def Compile(self, request_iterator, context):
        l.LOGGER('Go to compile a proyect.')
        input = grpcbf.parse_from_buffer(
            request_iterator = request_iterator,
            indices = gateway_pb2.CompileInput,
            partitions_model=[Buffer.Head.Partition(index={1: Buffer.Head.Partition()}), Buffer.Head.Partition(index={2: Buffer.Head.Partition()})],
            partitions_message_mode=[False, True]
        )
        if next(input) != gateway_pb2.CompileInput: raise Exception('Compile Input wrong.')
        for b in compile(
            repo = next(input),
            partitions_model = next(input)
        ): yield b

    def GetServiceTar(self, request_iterator, context):
        # TODO se debe de hacer que gestione mejor tomar todo el servicio, como hace GetServiceEstimatedCost.
        
        l.LOGGER('Request for give a service container.')
        for r in grpcbf.parse_from_buffer(
            request_iterator = request_iterator, 
            indices = GetServiceTar_input,
            partitions_message_mode=True
            ):

            # Si me da hash, comprueba que sea sha256 y que se encuentre en el registro.
            if type(r) is celaut.Any.Metadata.HashTag.Hash and SHA3_256_ID == r.type:
                hash = r.value.hex()
                break
            
            # Si me da servicio.
            if type(r) is celaut.Any:
                hash = get_service_hex_main_hash(service_buffer = r.value)
                save_service(  # TODO
                    service_p1 = r.value,
                    metadata = r.metadata
                )
                service_buffer = r.value
                break

        l.LOGGER('Getting the container of service ' + hash)
        if hash and hash in [s for s in os.listdir(REGISTRY)]:
            try:
                os.system('docker save ' + hash + '.service > ' + HYCACHE + hash + '.tar')
                l.LOGGER('Returned the tar container buffer.')
                yield utils.get_file_chunks(filename = HYCACHE + hash + '.tar')
            except:
                l.LOGGER('Error saving the container ' + hash)
        else:
            # Puede buscar el contenedor en otra red distinta a la del solicitante.
            try:
                yield search_container(
                    ignore_network = utils.get_network_name(
                        ip_or_uri = utils.get_only_the_ip_from_context(context_peer = context.peer())
                        ),
                    service_buffer = service_buffer
                )
            except:
                l.LOGGER('The service ' + hash + ' was not found.')

        yield gateway_pb2.buffer__pb2.Buffer(separator=True)
        raise Exception('Was imposible get the service container.')


    # Estimacion de coste de ejecución de un servicio con la cantidad de gas por defecto.
    def GetServiceEstimatedCost(self, request_iterator, context):
        # TODO podría comparar costes de otros pares, (menos del que le pregunta.)

        l.LOGGER('Request for the cost of a service.')
        parse_iterator = grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices = GetServiceEstimatedCost_input,
            partitions_model={2: StartService_input_partitions_v2[2]},
            partitions_message_mode={1: True, 2: [True, False]}
        )
        while True:
            try:
                r = next(parse_iterator)
            except StopIteration: break
            cost = None
            if type(r) is celaut.Any.Metadata.HashTag.Hash and SHA3_256_ID == r.type:
                if r.value.hex() in [s for s in os.listdir(REGISTRY)]:
                    yield gateway_pb2.buffer__pb2.Buffer(signal = True)
                    try:
                        p1 = get_from_registry(
                                    hash = r.value.hex()
                                )
                        cost = execution_cost(
                                service_buffer = p1.value,
                                metadata = p1.metadata
                            ) * GAS_COST_FACTOR
                        break
                    except build.UnsupportedArquitectureException as e: raise e
                    except Exception as e:
                        yield gateway_pb2.buffer__pb2.Buffer(signal = True)
                        continue
                elif DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH: raise Exception("I dont've the service.")

            if r is gateway_pb2.ServiceWithMeta:
                if DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH: raise Exception("I dont've the service.")
                service_with_meta = next(parse_iterator)
                second_partition_dir = next(parse_iterator)
                if type(second_partition_dir) is not str: raise Exception('Error: fail sending service.')
                try:
                    cost = execution_cost(
                            service_buffer = get_service_buffer_from_registry(
                                hash = save_service(
                                    service_p1 = service_with_meta.service.SerializeToString(),
                                    service_p2 = second_partition_dir,
                                    metadata = service_with_meta.metadata,
                                    hash = None
                                )
                            ),
                            metadata = service_with_meta.metadata
                        ) * GAS_COST_FACTOR
                except build.UnsupportedArquitectureException as e: raise e
                break
        
        cost = cost + default_cost(father_ip = utils.get_only_the_ip_from_context(context_peer = context.peer()))
        l.LOGGER('Execution cost for a service is requested, cost -> ' + str(cost) + ' with benefit ' + str(0))
        if cost is None: raise Exception("I dont've the service.")
        for b in grpcbf.serialize_to_buffer(
            message_iterator = gateway_pb2.EstimatedCost(
                                cost = utils.to_gas_amount(cost),
                                variance = 0.01  # TODO dynamic variance.
                            ),
            indices = gateway_pb2.EstimatedCost
        ): yield b


    def Payable(self, request_iterator, context):
        l.LOGGER('Request for payment.')
        payment = next(grpcbf.parse_from_buffer(
            request_iterator = request_iterator,
            indices = gateway_pb2.Payment,
            partitions_message_mode = True
        ))
        if not validate_payment_process(
            peer = context.peer(),
            amount = utils.from_gas_amount(payment.gas_amount),
            ledger = payment.contract_ledger.ledger,
            contract = payment.contract_ledger.contract,
            contract_addr = payment.contract_ledger.contract_addr,
            token = payment.deposit_token,
        ): raise Exception('Error: payment not valid.')
        l.LOGGER('Payment is valid.')
        for b in grpcbf.serialize_to_buffer(): yield b

    
    def GetMetrics(self, request_iterator, context):
        for b in grpcbf.serialize_to_buffer(
            message_iterator = get_metrics(
                token = next(grpcbf.parse_from_buffer(
                    request_iterator = request_iterator,
                    indices = gateway_pb2.TokenMessage,
                    partitions_message_mode=True
                )).token
            ),
            indices = gateway_pb2.Metrics,
        ): yield b


if __name__ == "__main__":
    # Create __hycache__ if it does not exists.
    try:
        os.system('mkdir ' + HYCACHE)
    except:
        pass
    
       # Create __registry__ if it does not exists.
    try:
        os.system('mkdir ' + REGISTRY)
    except:
        pass

    from zeroconf import Zeroconf
    import iobigdata
    from psutil import virtual_memory
    
    iobigdata.IOBigData(
        ram_pool_method = lambda: virtual_memory().total
    ).set_log(
        log = l.LOGGER if MEMORY_LOGS else lambda message: None
    )

    grpcbf.modify_env(
        cache_dir = HYCACHE,
        mem_manager = iobigdata.mem_manager
        )

    # Zeroconf for connect to the network (one per network).
    for network in ni.interfaces():
        if network != DOCKER_NETWORK and network != LOCAL_NETWORK:
            Zeroconf(network=network)

    # Run manager.
    threading.Thread(
        target = manager_thread,
        daemon = True
    ).start()

    # create a gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=30))
    gateway_pb2_grpc.add_GatewayServicer_to_server(
        Gateway(), server=server
    )

    SERVICE_NAMES = (
        gateway_pb2.DESCRIPTOR.services_by_name['Gateway'].full_name,
    )

    server.add_insecure_port('[::]:' + str(GATEWAY_PORT))
   
    l.LOGGER('COMPUTE POWER RATE -> '+ str(COMPUTE_POWER_RATE))
    l.LOGGER('COST OF BUILD -> '+ str(COST_OF_BUILD))
    l.LOGGER('EXECUTION BENEFIT -> '+ str(EXECUTION_BENEFIT))
    l.LOGGER('IGNORE FATHER NETWORK ON SERVICE BALANCER -> '+ str(IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER))
    l.LOGGER('SEND ONLY HASHES ASKING COST -> '+ str(SEND_ONLY_HASHES_ASKING_COST))
    l.LOGGER('DENEGATE COST REQUEST IF DONT VE THE HASH -> '+ str(DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH))
    l.LOGGER('MANAGER ITERATION TIME-> '+ str(MANAGER_ITERATION_TIME))
    l.LOGGER('AVG COST MAX PROXIMITY FACTOR-> '+ str(COST_AVERAGE_VARIATION))
    l.LOGGER('GAS_COST_FACTOR-> '+ str(GAS_COST_FACTOR))
    l.LOGGER('MODIFY_SERVICE_SYSTEM_RESOURCES_COST_FACTOR-> '+ str(MODIFY_SERVICE_SYSTEM_RESOURCES_COST))

    l.LOGGER('Starting gateway at port'+ str(GATEWAY_PORT))    

    server.start()
    server.wait_for_termination()

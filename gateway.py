from typing import Generator

import celaut_pb2 as celaut
import build, utils
from compile import REGISTRY, HYCACHE, compile
import logger as l
from verify import SHA3_256_ID, check_service, get_service_hex_main_hash
import subprocess, os, threading, shutil
import grpc, gateway_pb2, gateway_pb2_grpc
from concurrent import futures
from grpc_reflection.v1alpha import reflection
import pymongo, json
from google.protobuf.json_format import MessageToJson
from google.protobuf.json_format import Parse
import docker as docker_lib
import netifaces as ni
from gateway_pb2_grpcbf import StartService_input, GetServiceCost_input, GetServiceTar_input, StartService_input_partitions_v1, StartService_input_partitions_v2
import grpcbigbuffer as grpcbf
import iobigdata as iobd

DOCKER_CLIENT = lambda: docker_lib.from_env()
DOCKER_NETWORK = 'docker0'
LOCAL_NETWORK = 'lo'
GATEWAY_PORT = utils.GET_ENV(env = 'GATEWAY_PORT', default = 8090)
COMPUTE_POWER_RATE = utils.GET_ENV(env = 'COMPUTE_POWER_RATE', default = 2)
COST_OF_BUILD = utils.GET_ENV(env = 'COST_OF_BUILD', default = 5)
EXECUTION_BENEFIT = utils.GET_ENV(env = 'EXECUTION_BENEFIT', default = 1)
MEMORY_LOGS = utils.GET_ENV(env = 'MEMORY_LOGS', default = False)
IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER = utils.GET_ENV(env = 'IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER', default = False)
SEND_ONLY_HASHES_ASKING_COST = utils.GET_ENV(env = 'SEND_ONLY_HASHES_ASKING_COST', default=True)

def generate_gateway_instance(network: str) -> gateway_pb2.Instance:
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
    return gateway_pb2.Instance(
        instance = instance
    )

# Insert the instance if it does not exists.
def insert_instance_on_mongo(instance: celaut.Instance):
    parsed_instance = json.loads(MessageToJson(instance))
    pymongo.MongoClient(
        "mongodb://localhost:27017/"
    )["mongo"]["peerInstances"].update_one(
        filter = parsed_instance,
        update={'$setOnInsert': parsed_instance},
        upsert = True
    )

cache_lock = threading.Lock()
cache = {}  # ip:[dependencies]

# internal token -> str( peer_ip##container_ip##container_id )   peer_ip se refiere a la direccion del servicio padre (que puede ser interno o no).
# external token -> str( peer_ip##node_ip:node_port##his_token )

# En caso de mandarle la tarea a otro nodo:
#   En cache se añadirá el servicio como dependencia del nodo elegido,
#   en vez de usar la ip del servicio se pone el token que nos dió ese servicio,
#   nosotros a nuestro servicio solicitante le daremos un token con el formato node_ip##his_token.


def set_on_cache( father_ip : str, id_or_token: str, ip_or_uri: str):
    

    # En caso de ser un nodo externo:
    if not father_ip in cache:
        cache_lock.acquire()
        cache.update({father_ip: []})
        cache_lock.release()
        # Si peer_ip es un servicio del nodo ya
        # debería estar en el registro.


    # Añade el nuevo servicio como dependencia.
    cache[father_ip].append(ip_or_uri + '##' + id_or_token)
    l.LOGGER('Set on cache ' + ip_or_uri + '##' + id_or_token + ' as dependency of ' + father_ip )


def purgue_internal(father_ip, container_id, container_ip):
    try:
        DOCKER_CLIENT().containers.get(container_id).remove(force=True)
    except docker_lib.errors.APIError as e:
        l.LOGGER(str(e) + 'ERROR WITH DOCKER WHEN TRYING TO REMOVE THE CONTAINER ' + container_id)

    cache_lock.acquire()

    try:
        cache[father_ip].remove(container_ip + '##' + container_id)
    except ValueError as e:
        l.LOGGER(str(e) + str(cache[father_ip]) + ' trying to remove ' + container_ip + '##' + container_id)
    except KeyError as e:
        l.LOGGER(str(e) + father_ip + ' not in ' + str(cache.keys()))

    if container_ip in cache:
        for dependency in cache[container_ip]:
            # Si la dependencia esta en local.
            if utils.get_network_name(ip_or_uri = dependency.split('##')[0]) == DOCKER_NETWORK:
                purgue_internal(
                    father_ip = container_ip,
                    container_id = dependency.split('##')[1],
                    container_ip = dependency.split('##')[0]
                )
            # Si la dependencia se encuentra en otro nodo.
            else:
                purgue_external(
                    father_ip = father_ip,
                    node_uri = dependency.split('##')[0],
                    token = dependency[len(dependency.split('##')[0]) + 1:] # Por si el token comienza en # ...
                )

        try:
            l.LOGGER('Deleting the instance ' + container_id + ' from cache with ' + str(cache[container_ip]) + ' dependencies.')
            del cache[container_ip]
        except KeyError as e:
            l.LOGGER(str(e) + container_ip + ' not in ' + str(cache.keys()))

    cache_lock.release()


def purgue_external(father_ip, node_uri, token):
    # EN NODE_URI LLEGA UNA IP ¿?
    if len(node_uri.split(':')) < 2:
        l.LOGGER('Should be an uri not an ip. Something was wrong. The node uri is ' + node_uri)
        return None
    
    cache_lock.acquire()
    
    try:
        cache[father_ip].remove(node_uri + '##' + token)
    except ValueError as e:
        l.LOGGER(str(e) + str(cache[father_ip]) + ' trying to remove ' + node_uri + '##' + token)
    except KeyError as e:
        l.LOGGER(str(e) + father_ip + ' not in ' + str(cache.keys()))

    # Le manda al otro nodo que elimine esa instancia.
    try:
        next(grpcbf.client_grpc(
            method = gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(
                            node_uri
                        )
                    ).StopService,
            input = gateway_pb2.TokenMessage(
                token = token
            ),
            indices_parser = gateway_pb2.Empty,
        ))
    except grpc.RpcError as e:
        l.LOGGER('Error during remove a container on ' + node_uri + ' ' + str(e))

    cache_lock.release()


def set_config(container_id: str, config: celaut.Configuration, api: celaut.Service.Api.Config):
    __config__ = celaut.ConfigurationFile()
    __config__.gateway.CopyFrom(generate_gateway_instance(network=DOCKER_NETWORK).instance)
    __config__.config.CopyFrom(config)
    os.mkdir(HYCACHE + container_id)
    # TODO: Check if api.format is valid or make the serializer for it.
    path = ''
    for e in api.path:
        path += '/' + e
    with open(HYCACHE + container_id + path, 'wb') as file:
        file.write(__config__.SerializeToString())
    while 1:
        try:
            subprocess.run(
                '/usr/bin/docker cp ' + HYCACHE + container_id + '/__config__ ' + container_id + ':/__config__',
                shell=True
            )
            break
        except subprocess.CalledProcessError as e:
            l.LOGGER(e.output)
    os.remove(HYCACHE + container_id + '/__config__')
    os.rmdir(HYCACHE + container_id)


def create_container(id: str, entrypoint: str, use_other_ports=None) -> docker_lib.models.containers.Container:
    try:
        return DOCKER_CLIENT().containers.create(
            image = id + '.docker', # https://github.com/moby/moby/issues/20972#issuecomment-193381422
            entrypoint = entrypoint,
            ports = use_other_ports
        )
    except docker_lib.errors.ImageNotFound:
        l.LOGGER('IMAGE WOULD BE IN DOCKER REGISTRY. BUT NOT FOUND.')     # LOS ERRORES DEBERIAN LANZAR ALGUN TIPO DE EXCEPCION QUE LLEGUE HASTA EL GRPC.
    except docker_lib.errors.APIError:
        l.LOGGER('DOCKER API ERROR ')

def build_cost(service_buffer: bytes, metadata: celaut.Any.Metadata) -> int:
    try:
        # Coste de construcción si no se posee el contenedor del servicio.
        # Debe de tener en cuenta el coste de buscar el conedor por la red.
        return sum([
            COST_OF_BUILD * get_service_hex_main_hash(service_buffer = service_buffer, metadata = metadata) \
                in [img.tags[0].split('.')[0] for img in DOCKER_CLIENT().images.list()] is False,
            # Coste de obtener el contenedor ... #TODO
            ])
    except:
        pass
    return 0

def execution_cost(service_buffer: bytes, metadata: celaut.Any.Metadata) -> int:
    return sum([
        len( DOCKER_CLIENT().containers.list() )* COMPUTE_POWER_RATE,
        build_cost(service_buffer = service_buffer, metadata = metadata),
    ]) 

def service_balancer(service_buffer: bytes, metadata: celaut.Any.Metadata, ignore_network: str = None) -> dict: # sorted by cost, dict of celaut.Instances or 'local'  and cost.
    class PeerCostList:
        # Sorts the list from the element with the smallest weight to the element with the largest weight.
        
        def __init__(self) -> None:
            self.dict = {} # elem : weight
        
        def add_elem(self, weight: int, elem: str = 'local' ) -> None:
            self.dict.update({elem: weight})
        
        def get(self) -> dict:
            return {k : v for k, v in sorted(self.dict.items(), key=lambda item: item[1])}

    try:
        peers = PeerCostList()
        peers.add_elem(
            weight = execution_cost(service_buffer = service_buffer, metadata = metadata)
        )

        for peer in utils.peers_iterator(ignore_network = ignore_network):
            # TODO could use async or concurrency.
            peer_uri = peer['ip']+':'+str(peer['port'])
            try:
                peers.add_elem(
                    elem = peer_uri,
                    weight = next(grpcbf.client_grpc(
                        method =  gateway_pb2_grpc.GatewayStub(
                                    grpc.insecure_channel(
                                        peer_uri
                                    )
                                ).GetServiceCost,
                        indices_parser = gateway_pb2.CostMessage,
                        partitions_message_mode_parser = True,
                        indices_serializer = GetServiceCost_input,
                        input = utils.service_extended(service_buffer = service_buffer, metadata = metadata, send_only_hashes = SEND_ONLY_HASHES_ASKING_COST),
                    )).cost
                )
            except Exception as e: l.LOGGER('Error taking the cost: '+str(e))

        return peers.get()

    except Exception as e:
        l.LOGGER('Error during balancer, ' + str(e))
        return {'local': 0}


def launch_service(
        service_buffer: bytes, 
        metadata: celaut.Any.Metadata, 
        father_ip: str, 
        id = None,
        config: celaut.Configuration = None
    ) -> gateway_pb2.Instance:
    l.LOGGER('Go to launch a service. ')
    if service_buffer == None: raise Exception("Service object can't be None")
    getting_container = False
    # Here it asks the balancer if it should assign the job to a peer.
    while True:
        for peer_instance_uri, cost in service_balancer(
            service_buffer = service_buffer,
            metadata = metadata,
            ignore_network = utils.get_network_name(
                ip_or_uri = father_ip
            ) if IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER else None
        ).items():
            l.LOGGER('Balancer select peer ' + str(peer_instance_uri) + ' with cost ' + str(cost))
            
            # Delegate the service instance execution.
            if peer_instance_uri != 'local':
                try:
                    l.LOGGER('El servicio se lanza en el nodo con uri ' + str(peer_instance_uri))
                    # TODO se debe hacer que al pedir un servicio exista un timeout.
                    service_instance = next(grpcbf.client_grpc(
                        method = gateway_pb2_grpc.GatewayStub(
                                    grpc.insecure_channel(
                                        peer_instance_uri
                                    )
                                ).StartService,
                        partitions_message_mode_parser = True,
                        indices_serializer = StartService_input,
                        indices_parser = gateway_pb2.Instance,
                        input = utils.service_extended(
                                service_buffer = service_buffer, 
                                metadata = metadata,
                                config = config
                            )
                    ))
                    set_on_cache(
                        father_ip = father_ip,
                        ip_or_uri =  peer_instance_uri, # Add node_uri.
                        id_or_token = service_instance.token  # Add token.
                    )
                    service_instance.token = father_ip + '##' + peer_instance_uri.split(':')[0] + '##' + service_instance.token  # TODO adapt for ipv6 too.
                    return service_instance
                except Exception as e:
                    l.LOGGER('Failed starting a service on peer, occurs the eror: ' + str(e))

            #  The node launches the service locally.
            if getting_container: l.LOGGER('El nodo lanza el servicio localmente.')
            try:
                id = build.build(
                        service_buffer = service_buffer, 
                        metadata = metadata,
                        id = id,
                        get_it = not getting_container
                    )  #  If the container is not built, build it.
            except:
                # If it does not have the container, it takes it from another node in the background and requests
                #  the instance from another node as well.
                getting_container = True
                continue

            # Now serialize the part of the service that is needed.
            service = celaut.Service()
            service.ParseFromString(service_buffer)
            # If the request is made by a local service.
            if utils.get_network_name(father_ip) == DOCKER_NETWORK:
                container = create_container(
                    id = id,
                    entrypoint = service.container.entrypoint
                )

                set_config(container_id = container.id, config = config, api = service.api.config)

                # The container must be started after adding the configuration file and
                #  before requiring its IP address, since docker assigns it at startup.

                try:
                    container.start()
                except docker_lib.errors.APIError as e:
                    l.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' '+str(e)) # TODO LOS ERRORES DEBERIAN LANZAR ALGUN TIPO DE EXCEPCION QUE LLEGUE HASTA EL GRPC.

                # Reload this object from the server again and update attrs with the new data.
                container.reload()
                container_ip = container.attrs['NetworkSettings']['IPAddress']

                set_on_cache(
                    father_ip = father_ip,
                    id_or_token = container.id,
                    ip_or_uri = container_ip
                )

                for slot in service.api.slot:
                    uri_slot = celaut.Instance.Uri_Slot()
                    uri_slot.internal_port = slot.port

                    # Since it is internal, we know that it will only have one possible address per slot.
                    uri = celaut.Instance.Uri()
                    uri.ip = container_ip
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
                set_config(container_id = container.id, config = config, api = service.api.config)
                try:
                    container.start()
                except docker_lib.errors.APIError as e:
                    # TODO LOS ERRORES DEBERÍAN LANZAR UNA EXCEPCION QUE LLEGUE HASTA EL GRPC.
                    l.LOGGER('ERROR ON CONTAINER '+ str(container.id) + ' '+str(e)) 

                # Reload this object from the server again and update attrs with the new data.
                container.reload()

                set_on_cache(
                    father_ip = father_ip,
                    id_or_token = container.id,
                    ip_or_uri = container.attrs['NetworkSettings']['IPAddress']
                )

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
                token = father_ip + '##' + container.attrs['NetworkSettings']['IPAddress'] + '##' + container.id,
                instance = celaut.Instance(
                        api = service.api,
                        uri_slot = [uri_slot]
                    )
            )


def save_service(
    service_p1: bytes, 
    service_p2: str,
    metadata: celaut.Any.Metadata, 
    hash: str = None
    ):
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

def search_container(
        service_buffer: bytes, 
        metadata: celaut.Any.Metadata,
        ignore_network: str = None
    ) -> Generator[gateway_pb2.buffer__pb2.Buffer, None, None]:
    # Search a service tar container.
    for peer in utils.peers_iterator(ignore_network = ignore_network):
        try:
            next(
                grpcbf.client_grpc(
                    method = gateway_pb2_grpc.GatewayStub(
                                grpc.insecure_channel(peer['ip'] + ':' + str(peer['port']))
                            ).GetServiceTar,
                    input = utils.service_extended(
                                service_buffer = service_buffer,
                                metadata = metadata
                            ),
                    indices_serializer = GetServiceTar_input
                )
            )
            break
        except: pass

def search_file(hashes: list, ignore_network: str = None) -> Generator[celaut.Any, None, None]:
    # TODO: It can search for other 'Service ledger' or 'ANY ledger' instances that could've this type of files.
    for peer in  utils.peers_iterator(ignore_network = ignore_network):
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
        if any.metadata.complete:
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
            any.ParseFromString(iobd.read_file(filename = first_partition_dir))
            return any
    except (IOError, FileNotFoundError):
        l.LOGGER('The service was not on registry.')
        raise FileNotFoundError


class Gateway(gateway_pb2_grpc.Gateway):

    def StartService(self, request_iterator, context):
        l.LOGGER('Starting service ...')
        configuration = None
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

            # Captura la configuracion si puede.
            elif type(r) is celaut.Configuration:
                configuration = r
            
            elif type(r) is celaut.Any.Metadata.HashTag.Hash:
                hash = r

            # Si me da hash, comprueba que sea sha256 y que se encuentre en el registro.
            if hash:
                hashes.append(hash)
                if configuration and SHA3_256_ID == hash.type and \
                    hash.value.hex() in [s for s in os.listdir(REGISTRY)]:
                    yield gateway_pb2.buffer__pb2.Buffer(signal = True)
                    try:
                        metadata = celaut.Any.Metadata()
                        metadata.hashtag.hash.append(hash)
                        metadata.complete = True # TODO check
                        for b in grpcbf.serialize_to_buffer(
                            indices={},
                            message_iterator = launch_service(
                                service_buffer = get_service_buffer_from_registry(
                                    hash = hash.value.hex()
                                ),
                                metadata = metadata, 
                                config = configuration,
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
                if second_partition_dir[-2:] != 'p2': raise Exception('Invalid partition for service ', second_partition_dir)
                #service_on_any.metadata.complete = False  # TODO: this should?
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
                    father_ip = utils.get_only_the_ip_from_context(context_peer = context.peer())
                )
            ): yield b

        except Exception as e:
            raise Exception('Was imposible start the service. ' + str(e))


    def StopService(self, request_iterator, context):
        token_message = next(grpcbf.parse_from_buffer(
            request_iterator = request_iterator,
            indices = gateway_pb2.TokenMessage,
            partitions_message_mode=True
        ))

        l.LOGGER('Stopping the service with token ' + token_message.token)
        
        if utils.get_network_name(ip_or_uri = token_message.token.split('##')[1]) == DOCKER_NETWORK: # Suponemos que no tenemos un token externo que empieza por una direccion de nuestra subnet.
            purgue_internal(
                father_ip = token_message.token.split('##')[0],
                container_id = token_message.token.split('##')[2],
                container_ip = token_message.token.split('##')[1]
            )
        
        else:
            purgue_external(
                father_ip = token_message.token.split('##')[0],
                node_uri = token_message.token.split('##')[1],
                token = token_message.token[len( token_message.token.split('##')[1] ) + 1:] # Por si el token comienza en # ...
            )
        
        l.LOGGER('Stopped the instance with token -> ' + token_message.token)
        yield gateway_pb2.buffer__pb2.Buffer(
            chunk = gateway_pb2.Empty().SerializeToString(),
            separator = True
        )
    
    def Hynode(self, request_iterator, context):
        instance = next(grpcbf.parse_from_buffer(
            request_iterator = request_iterator,
            indices = gateway_pb2.Instance,
            partitions_message_mode = True
        ))
        l.LOGGER('\nAdding peer ' + str(instance))
        insert_instance_on_mongo(instance = instance.instance)

        for b in grpcbf.serialize_to_buffer(
            generate_gateway_instance(
                network = utils.get_network_name(
                    ip_or_uri = utils.get_only_the_ip_from_context(
                        context_peer = context.peer()
                    )
                )
            )            
        ): yield b


    def GetFile(self, request_iterator, context):
        l.LOGGER('Request for give a service definition')
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
        input = next(grpcbf.parse_from_buffer(
            request_iterator = request_iterator,
            indices = gateway_pb2.CompileInput,
            partitions_message_mode=True
        ))
        for b in compile(
            repo = input.repo,
            partitions_model = input.partitions_model
        ): yield b

    def GetServiceTar(self, request_iterator, context):
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


    def GetServiceCost(self, request_iterator, context):
        # TODO podría comparar costes de otros pares, (menos del que le pregunta.)
        for r in grpcbf.parse_from_buffer(
            request_iterator=request_iterator,
            indices = GetServiceCost_input,
            partitions_message_mode=True
        ):
            cost = None
            if type(r) is celaut.Any.Metadata.HashTag.Hash and SHA3_256_ID == r.type and \
                r.value.hex() in [s for s in os.listdir(REGISTRY)]:
                yield gateway_pb2.buffer__pb2.Buffer(signal = True)
                try:
                    metadata = celaut.Any.Metadata()
                    metadata.hashtag.hash.append(r)
                    cost = execution_cost(
                            service_buffer = get_service_buffer_from_registry(
                                    hash = r.value.hex()
                                ),
                            metadata = metadata
                        )
                    break
                except Exception as e:
                    yield gateway_pb2.buffer__pb2.Buffer(signal = True)
                    continue

            if type(r) is celaut.Any:
                cost = execution_cost(
                    service_buffer = r.value,
                    metadata = r.metadata
                )
                break
                
        if not cost: raise Exception("I dont've the service.")
        l.LOGGER('Execution cost for a service is requested, cost -> ' + str(cost))
        for b in grpcbf.serialize_to_buffer(
            message_iterator = gateway_pb2.CostMessage(
                                cost = cost + EXECUTION_BENEFIT
                            ),
            indices = gateway_pb2.CostMessage      
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
    
    iobigdata.IOBigData().set_log(
        log = l.LOGGER if MEMORY_LOGS else None
        )

    grpcbf.modify_env(
        cache_dir = HYCACHE,
        mem_manager = iobigdata.mem_manager
        )

    # Zeroconf for connect to the network (one per network).
    for network in ni.interfaces():
        if network != DOCKER_NETWORK and network != LOCAL_NETWORK:
            Zeroconf(network=network)

    # create a gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=30))
    gateway_pb2_grpc.add_GatewayServicer_to_server(
        Gateway(), server=server
    )

    SERVICE_NAMES = (
        gateway_pb2.DESCRIPTOR.services_by_name['Gateway'].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(SERVICE_NAMES, server)

    server.add_insecure_port('[::]:' + str(GATEWAY_PORT))
    l.LOGGER('Starting gateway at port'+ str(GATEWAY_PORT))
    server.start()
    server.wait_for_termination()

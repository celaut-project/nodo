from ipss_pb2 import Slot
import build, utils
from compile import REGISTRY, HYCACHE
import logger as l
from verify import get_service_hash
import subprocess, os, socket, threading, random
import grpc, gateway_pb2, gateway_pb2_grpc
from concurrent import futures
from grpc_reflection.v1alpha import reflection
import pymongo, json
from google.protobuf.json_format import MessageToJson
from google.protobuf.json_format import Parse
import docker as docker_lib
import netifaces as ni

DOCKER_CLIENT = docker_lib.from_env()
DOCKER_NETWORK = 'docker0'
LOCAL_NETWORK = 'lo'
GATEWAY_PORT = 8080


def generate_gateway_instance(network: str) -> gateway_pb2.ipss__pb2.Instance:
    instance = gateway_pb2.ipss__pb2.Instance()

    uri = gateway_pb2.ipss__pb2.Instance.Uri()
    try:
        uri.ip = ni.ifaddresses(network)[ni.AF_INET][0]['addr']
    except ValueError as e:
        l.LOGGER('You must specify a valid interface name ' + network)
        raise Exception('Error generating gateway instance --> ' + str(e))        
    uri.port = GATEWAY_PORT
    uri_slot = gateway_pb2.ipss__pb2.Instance.Uri_Slot()
    uri_slot.internal_port = GATEWAY_PORT
    uri_slot.uri.append(uri)
    instance.uri_slot.append(uri_slot)
    
    slot = gateway_pb2.ipss__pb2.Slot()
    slot.port = GATEWAY_PORT
    slot.transport_protocol.hash.extend(['http2', 'grpc'])
    instance.api.slot.append(slot)
    return instance

# Insert the instance if it does not exists.
def insert_instance_on_mongo(instance: gateway_pb2.ipss__pb2.Instance):
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


def purgue_internal(father_ip, container_id, container_ip):
    try:
        DOCKER_CLIENT.containers.get(container_id).remove(force=True)
    except docker_lib.errors.APIError:
        l.LOGGER('ERROR WITH DOCKER WHEN TRYING TO REMOVE THE CONTAINER ' + container_id)

    cache_lock.acquire()

    try:
        cache[father_ip].remove(container_ip + '##' + container_id)
    except ValueError as e:
        l.LOGGER(str(e) + str(cache[father_ip]) + 'trying to remove ' + container_ip + '##' + container_id)

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
            del cache[container_ip]
        except KeyError as e:
            l.LOGGER(str(e) + container_ip + ' not in ' + str(cache.keys()))

    cache_lock.release()


def purgue_external(father_ip, node_uri, token):
    cache_lock.acquire()

    try:
        cache[father_ip].remove(node_uri + '##' + token)
    except ValueError as e:
        l.LOGGER(str(e) + str(cache[father_ip]) + 'trying to remove ' + node_uri + '##' + token)

    # Le manda al otro nodo que elimine esa instancia.
    try:
        gateway_pb2_grpc.Gateway(
            grpc.insecure_channel(node_uri)
        ).StopService(
             gateway_pb2.TokenMessage(
                token = token
            )
        )
    except grpc.RpcError as e:
        l.LOGGER('Error during remove a container on ' + node_uri + ' ' + str(e))

    cache_lock.release()


def set_config(container_id: str, config: gateway_pb2.ipss__pb2.Configuration):
    __config__ = gateway_pb2.ipss__pb2.ConfigurationFile()
    __config__.gateway.CopyFrom(generate_gateway_instance(network=DOCKER_NETWORK))
    __config__.config.CopyFrom(config)
    os.mkdir(HYCACHE + container_id)
    with open(HYCACHE + container_id + '/__config__', 'wb') as file:
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
        return DOCKER_CLIENT.containers.create(
            image = id+'.service',
            entrypoint = entrypoint,
            ports = use_other_ports
        )
    except docker_lib.errors.ImageNotFound:
        l.LOGGER('IMAGE WOULD BE IN DOCKER REGISTRY. BUT NOT FOUND.')     # LOS ERRORES DEBERIAN LANZAR ALGUN TIPO DE EXCEPCION QUE LLEGUE HASTA EL GRPC.
    except docker_lib.errors.APIError:
        l.LOGGER('DOCKER API ERROR ')


def service_balancer():
    try:
        peer_list = list(pymongo.MongoClient(
                        "mongodb://localhost:27017/"
                    )["mongo"]["peerInstances"].find())
        peer_list_length = len(peer_list)
        l.LOGGER('    Peer list length of ' + str(peer_list_length))

        i = random.randint(0, peer_list_length*2) # Tiene mas probabilidades de que toque en local que en un nodo externo.
        l.LOGGER('    Using the peer ' + str(i))
        if i < peer_list_length:
            del peer_list[i]['_id']
            return Parse(
                text = json.dumps(peer_list[i]),
                message = gateway_pb2.ipss__pb2.Instance(),
                ignore_unknown_fields = True
            )
        else:
            return None

    except Exception as e:
        l.LOGGER('Error during balancer, ' + str(e))
        return None


def launch_service(service: gateway_pb2.ipss__pb2.Service, config: gateway_pb2.ipss__pb2.Configuration, father_ip: str):
    l.LOGGER('\nGo to launch a service.')

    # Aqui le tiene pregunta al balanceador si debería asignarle el trabajo a algun par.
    node_instance = service_balancer()
    l.LOGGER('\nBalancer select peer ' + str(node_instance))
    if node_instance:
        try:
            node_uri = utils.get_grpc_uri(node_instance)
            l.LOGGER('El servicio se lanza en el nodo con uri ' + str(node_uri))
            service_instance =  gateway_pb2_grpc.GatewayStub(
                grpc.insecure_channel(
                    node_uri.ip + ':' +  str(node_uri.port)
                )
            ).StartService(
                utils.service_extended(service=service, config=config)
            )
            set_on_cache(
                father_ip = father_ip,
                ip_or_uri =  node_uri.ip + ':' +  str(node_uri.port), # Add node_uri.
                id_or_token = service_instance.token  # Add token.
            )
            service_instance.token = father_ip + '##' + node_uri.ip + '##' + service_instance.token
            return service_instance
        except Exception as e:
            l.LOGGER('Failed starting a service on peer, occurs the eror: ' + str(e))

    #  El nodo lanza localmente el servicio.
    l.LOGGER('El nodo lanza el servicio localmente.')
    build.build(service = service)  # Si no esta construido el contenedor, lo construye.
    instance = gateway_pb2.Instance()

    # Si hace la peticion un servicio local.
    if utils.get_network_name(father_ip) == DOCKER_NETWORK:
        container = create_container(
            id = get_service_hash(service = service, hash_type = "sha3-256"),
            entrypoint = service.container.entrypoint
        )

        set_config(container_id = container.id, config = config)

        # El contenedor se debe de iniciar tras añadir el fichero de configuración y 
        #  antes de requerir su direccion IP, puesto que docker se la asigna al inicio.

        try:
            container.start()
        except docker_lib.errors.APIError as e:
            l.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' '+str(e)) # LOS ERRORES DEBERIAN LANZAR ALGUN TIPO DE EXCEPCION QUE LLEGUE HASTA EL GRPC.

        # Reload this object from the server again and update attrs with the new data.
        container.reload()
        container_ip = container.attrs['NetworkSettings']['IPAddress']

        set_on_cache(
            father_ip = father_ip,
            id_or_token = container.id, 
            ip_or_uri = container_ip
        )

        for slot in service.api.slot:
            uri_slot = gateway_pb2.ipss__pb2.Instance.Uri_Slot()
            uri_slot.internal_port = slot.port

            # Al ser interno sabemos que solo tendrá una dirección posible por slot.
            uri = gateway_pb2.ipss__pb2.Instance.Uri()
            uri.ip = container_ip
            uri.port = slot.port
            uri_slot.uri.append(uri)

            instance.instance.uri_slot.append(uri_slot)

    # Si hace la peticion un servicio de otro nodo.
    else:
        assigment_ports = {slot.port: utils.get_free_port() for slot in service.api.slot}

        container = create_container(
            use_other_ports = assigment_ports,
            id = get_service_hash( service = service, hash_type = "sha3-256"),
            entrypoint = service.container.entrypoint
        )
        set_config(container_id = container.id, config = config)

        try:
            container.start()
        except docker_lib.errors.APIError as e:
            # LOS ERRORES DEBERÍAN LANZAR UNA EXCEPCION QUE LLEGUE HASTA EL GRPC.
            l.LOGGER('ERROR ON CONTAINER '+ str(container.id) + ' '+str(e)) 

        # Reload this object from the server again and update attrs with the new data.
        container.reload()

        set_on_cache(
            father_ip = father_ip,
            id_or_token = container.id,
            ip_or_uri = container.attrs['NetworkSettings']['IPAddress']
        )

        for port in assigment_ports:
            uri_slot = gateway_pb2.ipss__pb2.Instance.Uri_Slot()
            uri_slot.internal_port = port

            # for host_ip in host_ip_list:
            uri = gateway_pb2.ipss__pb2.Instance.Uri()
            uri.ip = utils.get_local_ip_from_network(
                network = utils.get_network_name(ip_or_uri=father_ip)
            )
            uri.port = assigment_ports[port]
            uri_slot.uri.append(uri)

            instance.instance.uri_slot.append(uri_slot)

    instance.instance.api.CopyFrom(service.api)
    instance.token = father_ip + '##' + container.attrs['NetworkSettings']['IPAddress'] + '##' + container.id
    l.LOGGER('Thrown out a new instance by ' + father_ip + ' of the container_id ' + container.id)
    return instance


def get_from_registry(hash):
    try:
        with open(REGISTRY + hash + '.service', 'rb') as file:
            service = gateway_pb2.ipss__pb2.Service()
            service.ParseFromString(file.read())
            return service
    except (IOError, FileNotFoundError) as e:
        l.LOGGER('Error opening the service on registry, ' + str(e))
        transport = gateway_pb2.ServiceTransport()
        transport.hash = hash

        #  Search the service description.
        peers = list(pymongo.MongoClient(
                    "mongodb://localhost:27017/"
                )["mongo"]["peerInstances"].find())
        for peer in peers:
            l.LOGGER('Looking for the service ' + hash + ' on peer ' + peer)
            peer_uri = peer['uriSlot'][0]['uri'][0]
            service = gateway_pb2_grpc.GatewayStub(
                grpc.insecure_channel(peer_uri['ip'] + ':' + peer_uri['port'])
            ).GetServiceDef(
                transport
            )
        
        if service:
            #  Save the service on the registry.
            with open(REGISTRY + hash + '.service', 'wb') as file:
                file.write(service.SerializeToString())

        else:
            l.LOGGER('The service '+ hash + ' was not found.')
            raise Exception('The service ' + hash + ' was not found.')

        return service

class Gateway(gateway_pb2_grpc.Gateway):

    def StartService(self, request_iterator, context):
        configuration = None
        service_registry = [service[:-8] for service in os.listdir(REGISTRY)]
        for r in request_iterator:

            # Captura la configuracion si puede.
            if r.HasField('config'):
                configuration = r.config
            
            # Si me da hash, comprueba que sea sha256 y que se encuentre en el registro.
            if r.HasField('hash') and configuration and "sha3-256" == r.hash.split(':')[0] \
                    and r.hash.split(':')[1] in service_registry:
                try:
                    return launch_service(
                        service = get_from_registry(r.hash.split(':')[1]),
                        config = configuration,
                        father_ip = utils.get_only_the_ip_from_context(context_peer=context.peer())
                    )
                except Exception as e:
                    l.LOGGER('Exception launching a service ' + str(e))
                    continue
            
            # Si me da servicio.
            if r.HasField('service') and configuration:
                # If the service is not on the registry, save it.
                hash = get_service_hash(service=r.service, hash_type="sha3-256")
                if not os.path.isfile(REGISTRY+hash+'.service'):
                    with open(REGISTRY+hash+'.service', 'wb') as file:
                        file.write(r.service.SerializeToString())
                
                return launch_service(
                    service = r.service,
                    config = configuration,
                    father_ip = utils.get_only_the_ip_from_context(context_peer=context.peer())
                )
        
        raise Exception('Was imposible start the service.')

    def StopService(self, request, context):
        
        if utils.get_network_name(ip_or_uri = request.token.split('##')[1]) == DOCKER_NETWORK: # Suponemos que no tenemos un token externo que empieza por una direccion de nuestra subnet.
            purgue_internal(
                father_ip = request.token.split('##')[0],
                container_id = request.token.split('##')[2],
                container_ip = request.token.split('##')[1]
            )
        
        else:
            purgue_external(
                father_ip = request.token.split('##')[0],
                node_uri = request.token.split('##')[1],
                token = request[len( request.token.split('##')[1] ) + 1:] # Por si el token comienza en # ...
            )
        
        l.LOGGER('Stopped the instance with token -> ' + request.token)
        return gateway_pb2.Empty()
    
    def Hynode(self, request: gateway_pb2.ipss__pb2.Instance, context):
        l.LOGGER('\nAdding peer ' + str(request))
        insert_instance_on_mongo(instance = request)
        return generate_gateway_instance(
            network = utils.get_network_name(
                ip_or_uri = utils.get_only_the_ip_from_context(
                    context_peer = context.peer()
                )
            )
        )

    def GetServiceDef(self, request_iterator, context):
        l.LOGGER('Request for give a service definition')
        service_registry = [service[:-8] for service in os.listdir(REGISTRY)]
        for r in request_iterator:

            try:
                # Si me da hash, comprueba que sea sha256 y que se encuentre en el registro.
                if r.HasField('hash') and "sha3-256" == r.hash.split(':')[0] \
                        and r.hash.split(':')[1] in service_registry:
                    return get_from_registry(
                        hash = r.hash.split(':')[1]
                    )
                
                # Si me da servicio.
                if r.HasField('service'):
                    hash = get_service_hash(service = r.service, hash_type="sha3-256") 
                    if hash in service_registry:
                        return get_from_registry(hash = hash)
            except:
                pass

        raise Exception('Was imposible get the service definition.')

    def GetServiceTar(self, request_iterator, context):
        l.LOGGER('Request for give a service container.')
        service_registry = [service[:-8] for service in os.listdir(REGISTRY)]
        for r in request_iterator:

            # Si me da hash, comprueba que sea sha256 y que se encuentre en el registro.
            if r.HasField('hash') and "sha3-256" == r.hash.split(':')[0] \
                    and r.hash.split(':')[1] in service_registry:
                hash = r.hash.split(':')[1]
                break
            
            # Si me da servicio.
            if r.HasField('service'):
                try:
                    hash = get_service_hash(service=r.service, hash_type="sha3-256")
                except:
                    continue
                if hash in service_registry:
                    break
        l.LOGGER('Getting the container of service ' + hash)
        if hash:
            try:
                os.system('docker save ' + hash + '.service > ' + HYCACHE + hash + '.tar')
                l.LOGGER('Returned the tar container buffer.')
                return utils.get_file_chunks(filename = HYCACHE + hash + '.tar')
            except:
                l.LOGGER('Error saving the container ' + hash)
        else:
            l.LOGGER('The service '+ hash + ' was not found.')

        raise Exception('Was imposible get the service container.')

if __name__ == "__main__":
    from zeroconf import Zeroconf

    # Create __hycache__ if it does not exists.
    try:
        os.system('mkdir ' + HYCACHE)
    except:
        pass

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

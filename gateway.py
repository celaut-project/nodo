import build
from compile import REGISTRY, HYCACHE, LOGGER
from verify import get_service_hash
import subprocess, os, socket, threading
import grpc, gateway_pb2, gateway_pb2_grpc
from concurrent import futures

IS_FROM_DOCKER_SUBNET = lambda s: s[:7] == '172.17.'

GATEWAY_INSTANCE = gateway_pb2.ipss__pb2.Instance()
GATEWAY_PORT = 8080

cache_lock = threading.Lock()
cache = {}  # ip:[dependencies]


# internal token -> str( peer_ip:container_id:container_ip )
# external token -> str( node_ip:his_token )

# En caso de mandarle la tarea a otro nodo:
#   En cache se añadirá el servicio como dependencia del nodo elegido,
#   en vez de usar la ip del servico se pone el token que nos dió ese servicio,
#   nosotros a nuestro servicio solicitante le daremos un token con el formato node_ip:his_token.

def set_on_cache(peer_ip, container_id, container_ip):
    cache_lock.acquire()

    # En caso de ser un nodo externo:
    if not peer_ip in cache:
        cache.update({peer_ip: []})
        # Si peer_ip es un servicio del nodo ya
        # debería estar en el registro.

    # Añade el nuevo servicio como dependencia.
    cache[peer_ip].append(container_id + ':' + container_ip)

    # Añade el servicio creado en el registro.
    cache.update({container_ip: []})

    cache_lock.release()


def purgue_internal(peer_ip, container_id, container_ip):
    subprocess.run('/usr/bin/docker rm ' + container_id + ' --force', shell=True)
    cache_lock.acquire()
    cache[peer_ip].remove(container_id + ':' + container_ip)
    for dependency in cache[container_id]:
        # Si la dependencia esta en local.
        if IS_FROM_DOCKER_SUBNET(dependency.split(':')[0]):
            purgue_internal(
                peer_ip=container_id,
                container_id=dependency.split(':')[0],
                container_ip=dependency.split(':')[1]
            )
        # Si la dependencia se encuentra en otro nodo.
        else:
            purgue_external(
                node_ip=dependency.split(':')[0],
                token=dependency[len(dependency.split(':')[0]) + 1:]
            )
    cache_lock.release()


def purgue_external(node_ip, token):
    cache_lock.acquire()
    cache[node_ip].remove(token)
    # peer_stubs[node_ip](token)
    cache_lock.release()


def get_container_ip(container_id: str):
    while 1:
        try:
            return subprocess.check_output(
                "/usr/bin/docker inspect --format \"{{ .NetworkSettings.IPAddress }}\" " + container_id,
                shell=True
            ).decode('utf-8').replace('\n', '')
        except subprocess.CalledProcessError as e:
            LOGGER(e.output)


def set_config(container_id: str, config: gateway_pb2.ipss__pb2.Configuration):
    __config__ = gateway_pb2.ipss__pb2.ConfigurationFile()
    __config__.gateway.CopyFrom(GATEWAY_INSTANCE)
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
            LOGGER(e.output)
    os.read(HYCACHE + container_id + '/__config__')
    os.rmdir(HYCACHE + container_id)


def start_container(id: str, use_other_ports=None):
    command = '/usr/bin/docker run'
    if use_other_ports is not None:
        for port in use_other_ports:
            command = command + ' -p ' + str(use_other_ports[port]) + ':' + str(port)
    return subprocess.check_output(command + ' --detach ' + id+'.service', shell=True).decode('utf-8').replace('\n', '')


def launch_service(service: gateway_pb2.ipss__pb2.Service, config: gateway_pb2.ipss__pb2.Configuration, peer_ip: str):
    # Aqui le tiene pregunta al balanceador si debería asignarle el trabajo a algun par.
    # De momento lo hace el y ya.
    build.build(service=service)  # Si no esta construido el contenedor, lo construye.

    service_ports = [slot.port for slot in service.api]
    instance = gateway_pb2.Instance()
    instance.instance.api.extend(service.api)

    # Si se trata de un servicio local.
    if IS_FROM_DOCKER_SUBNET(peer_ip):
        container_id = start_container(
            id=get_service_hash(service=service, hash_type="sha3-256")
        )

        container_ip = get_container_ip(container_id=container_id)

        set_config(container_id=container_id, config=config)

        set_on_cache(
            peer_ip=peer_ip,
            container_id=container_id,
            container_ip=container_ip
        )

        for port in service_ports:
            uri_slot = gateway_pb2.ipss__pb2.Instance.Uri_Slot()
            uri_slot.internal_port = port

            # Al ser interno sabemos que solo tendrá una dirección posible por slot.
            uri = gateway_pb2.ipss__pb2.Instance.Uri()
            uri.ip = container_ip
            uri.port = port
            uri_slot.uri.append(uri)

            instance.instance.uri_slot.append(uri_slot)

    # Si se trata de un servicio de otro nodo.
    else:
        def get_free_port():
            with socket.socket() as s:
                s.bind(('', 0))
                return int(s.getsockname()[1])

        host_ip = socket.gethostbyname(socket.gethostname()) # Debería ser una lista.
        assigment_ports = {port: get_free_port() for port in service_ports}

        container_id = start_container(
            use_other_ports=assigment_ports,
            id=get_service_hash(service=service, hash_type="sha3-256")
        )

        container_ip = get_container_ip(container_id=container_id)

        set_config(container_id=container_id, config=config)

        set_on_cache(
            peer_ip=peer_ip,
            container_id=container_id,
            container_ip=container_ip
        )

        for port in assigment_ports:
            uri_slot = gateway_pb2.ipss__pb2.Instance.Uri_Slot()
            uri_slot.port = port

            # for host_ip in host_ip_list:
            uri = gateway_pb2.ipss__pb2.Instance.Uri()
            uri.ip = host_ip
            uri.port = assigment_ports[port]
            uri_slot.uri.append(uri)

            instance.instance.uri_slot.append(uri_slot)

    instance.token.value_string = peer_ip + ':' + container_id + ':' + container_ip
    return instance


def get_from_registry(hash):
    try:
        with open(REGISTRY + hash + '.service', 'rb') as file:
            service = gateway_pb2.ipss__pb2.Service()
            service.ParseFromString(file.read())
            return service
    except IOError:
        print("Service " + hash + " not accessible.")
        # search service in IPFS service.


if __name__ == "__main__":
    print('Starting server.')

    class Gateway(gateway_pb2_grpc.Gateway):

        def StartService(self, request_iterator, context):
            configuration = None
            service_registry = [service[:-8] for service in os.listdir('./__registry__')]
            for r in request_iterator:
                # Captura la configuracion si puede.
                if r.HasField('configuration'): configuration = r.configuration
                # Si me da hash, comprueba que sea sha256 y que se encuentre en el registro.
                if r.HasField('hash') and configuration and "sha3-256" in r.hash.split(':')[0] \
                        and r.hash.split(':')[1] in service_registry:
                    return launch_service(
                        service=get_from_registry(r.hash.split(':')[1]),
                        config=configuration,
                        peer_ip=context.peer()[5:]  # Lleva el formato 'ipv4:49.123.106.100:44420', no queremos 'ipv4:'.
                    )
                # Si me da servicio.
                if r.HasField('service') and configuration:
                    # If the service is not on the registry, save it.
                    hash = get_service_hash(service=r.service, hash_type="sha3-256")
                    if not os.path.isfile(REGISTRY+hash+'.service'):
                        with open(REGISTRY+hash+'.service') as file:
                            file.write(r.service.SerializeToString())
                    
                    return launch_service(
                        service=r.service,
                        config=configuration,
                        peer_ip=context.peer()[5:]  # Lleva el formato 'ipv4:49.123.106.100:44420', no queremos 'ipv4:'.
                    )

        def StopService(self, request, context):
            if IS_FROM_DOCKER_SUBNET(request.string):
                purgue_internal(
                    peer_ip=request.string.split(':')[0],
                    container_id=request.string.split(':')[1],
                    container_ip=request.string.split(':')[2]
                )
            else:
                purgue_external(
                    node_ip=request.string.split(':')[0],
                    token=request.string.split(':')[1]
                )
            return gateway_pb2.Empty()


    slot = gateway_pb2.ipss__pb2.Slot()
    slot.port = GATEWAY_PORT
    # ¡¡ Empty protocol_mesh and application_def !!
    GATEWAY_INSTANCE.api.append(slot)
    uri = gateway_pb2.ipss__pb2.Instance.Uri()
    uri.ip = '172.17.0.1'
    uri.port = GATEWAY_PORT
    uri_slot = gateway_pb2.ipss__pb2.Instance.Uri_Slot()
    uri_slot.internal_port = GATEWAY_PORT
    uri_slot.uri.append(uri)
    GATEWAY_INSTANCE.uri_slot.append(uri_slot)

    # create a gRPC server
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=30))
    gateway_pb2_grpc.add_GatewayServicer_to_server(
        Gateway(), server=server
    )

    print('Listening on port ' + str(GATEWAY_PORT))

    server.add_insecure_port('[::]:' + str(GATEWAY_PORT))
    server.start()
    server.wait_for_termination()

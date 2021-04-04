import json
import build
import subprocess, os
import grpc, gateway_pb2, gateway_pb2_grpc
from concurrent import futures
import logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

token_cache = {} # token : token del padre  ( si es tokenhoster es que no tiene padre..)

instance_cache = {}  # uri : token

def launch_service( service: gateway_pb2.ipss__pb2.Service, config: gateway_pb2.ipss__pb2.Configuration, peer_ip: str ):
    def start_container(config, use_other_ports=None):

        command = '/usr/bin/docker run'
        if config.HasField('enviroment_variables') is not None:
            for env in config.enviroment_variables:
                command = command +' -e '+env+'='+config.enviroment_variables[env]

        if use_other_ports is not None:
            for port in use_other_ports:
                command = command + ' -p '+use_other_ports[port]+':'+port

        return subprocess.check_output(command +' --detach '+launch_service+'.oci', shell=True).decode('utf-8').replace('\n', '')

    build.build(service=service) # Si no esta construido, lo construye.
    service_ports = [ slot.port for slot in service.api.slot ]
    LOGGER(str(service_ports))

    if service_ports is None:
        LOGGER('No retorna direccion, no hay api.')

        container_id = start_container(config=config)

        if peer_ip in instance_cache.keys():
            father_token = instance_cache.get(peer_ip)
        else:
            father_token = 'tokenhoster'
            instance_cache.update({peer_ip:'tokenhoster'})
        token_cache.update({container_id:father_token})
        while 1:
            try:
                container_ip = subprocess.check_output("/usr/bin/docker inspect --format \"{{ .NetworkSettings.IPAddress }}\" "+container_id , shell=True).decode('utf-8').replace('\n', '')
                LOGGER(str(container_ip))
                break
            except subprocess.CalledProcessError as e:
                LOGGER(e.output)


        instance_cache.update({container_ip:container_id})
        instance = gateway_pb2.Instance()
        instance.token.value_string = container_id
        return instance
    else:
        LOGGER('Retorna la uri para usar la api.'+ str(service_ports))

        # Si se trata de un servicio local.
        if (peer_ip)[:7] == '172.17.' or (peer_ip) == '127.0.0.1':
            
            container_id = start_container(config=config)

            if peer_ip in instance_cache.keys():
                father_token = instance_cache.get(peer_ip)
            else:
                father_token = 'tokenhoster'
                instance_cache.update({peer_ip:'tokenhoster'})
            token_cache.update({container_id:father_token})
            while 1:
                try:
                    container_ip = subprocess.check_output("/usr/bin/docker inspect --format \"{{ .NetworkSettings.IPAddress }}\" "+container_id , shell=True).decode('utf-8').replace('\n', '')
                    LOGGER(container_ip)
                    break
                except subprocess.CalledProcessError as e:
                    LOGGER(e.output)

            instance_cache.update({container_ip:container_id})
            instance = gateway_pb2.Instance()
            for port in service_ports:
                uri = gateway_pb2.ipss__pb2.Gateway.Uri()
                uri.direction = container_id
                uri.port = port
                instance.uris.append( port, uri)
            instance.token.value_string = container_id
            return instance

        # Si se trata de un servicio en otro nodo.
        elif (peer_ip)[:4] == '192.':
            def get_free_port():
                from socket import socket
                with socket() as s:
                    s.bind(('',0))
                    return int(s.getsockname()[1])

            import socket as s
            host_ip = s.gethostbyname( s.gethostname() )
            assigment_ports = { port:get_free_port() for port in service_ports }
            container_id = start_container( use_other_ports = assigment_ports, config=config)

            if peer_ip in instance_cache.keys():
                father_token = instance_cache.get(peer_ip)
            else:
                father_token = 'tokenhoster'
                instance_cache.update({peer_ip:'tokenhoster'})
            token_cache.update({container_id:father_token})
            while 1:
                try:
                    container_ip = subprocess.check_output("/usr/bin/docker inspect --format \"{{ .NetworkSettings.IPAddress }}\" "+container_id , shell=True).decode('utf-8').replace('\n', '')
                    LOGGER(str(container_ip))
                    break
                except subprocess.CalledProcessError as e:
                    LOGGER(e.output)

            instance_cache.update({container_ip:container_id})
            instance = gateway_pb2.Instance()
            for port in assigment_ports:
                uri = gateway_pb2.ipss__pb2.Gateway.Uri()
                uri.direction = host_ip
                uri.port = assigment_ports[port]
                instance.uris.append( port, uri)
            instance.token.value_string = container_id
            return instance
        else:
            LOGGER('THIS NETWORK IS NOT SUPPORTED')

def get_from_registry(hash):
    try:
        with open('./__registry__/'+hash+'.service', "rb") as file:
            file = gateway_pb2.ServiceFile()
            file.ParseFromString(file.read())
            return file.service
    except IOError:
        print("Service "+hash+" not accessible.")
        # search service in IPFS service.

if __name__ == "__main__":
   print('Starting server.')
   class Gateway(gateway_pb2_grpc.Gateway):
        def StartServiceWithExtended(self, request_iterator, context):
            configuration = None
            service_registry = [service for service in os.listdir('./__registry__')]
            for r in request_iterator:
                # Captura la configuracion si puede.
                if r.HasField('configuration'): configuration = r.configuration 
                # Si me da hash, comprueba que sea sha256 y que se encuentre en el registro.
                if r.HasField('hash') and configuration and r.hash.algorithm == "sha2_256" \
                 and r.hash.hash in service_registry:
                    return launch_service(
                        service = get_from_registry(r.hash.hash),
                        config = configuration,
                        peer_ip = context.peer()[5:]  # Lleva el formato 'ipv4:49.123.106.100:44420', no queremos 'ipv4:'.
                        )
                # Si me da servicio.
                if r.HasField('service') and configuration:
                    return launch_service(
                        service = r.service,
                        config = configuration,
                        peer_ip = context.peer()[5:]  # Lleva el formato 'ipv4:49.123.106.100:44420', no queremos 'ipv4:'.
                        )

        def StopService(self, request, context):   
            # Se puede usar una cache para la recursividad,
            #  para no estar buscando todo el tiempo lo mismo.
            token = request.string
            if token == 'tokenhoster':
                LOGGER('TOKENHOSTER NO SE TOCA')
            else:
                subprocess.check_output('/usr/bin/docker rm '+token+' --force', shell=True)
                for d in token_cache:
                    if token_cache.get(d) == token:
                        token(d)
                        del token_cache[d]
                return gateway_pb2.Empty()

   print('Imported libs.')

   # create a gRPC server
   server = grpc.server(futures.ThreadPoolExecutor(max_workers=30))
   gateway_pb2_grpc.add_GatewayServicer_to_server(
       Gateway(), server=server
   )

   print('Listening on port 8000.')

   server.add_insecure_port('[::]:8000')
   server.start()
   server.wait_for_termination()

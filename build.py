from time import sleep
import threading
from gateway_pb2_grpc_indices import GetServiceTar_input_indices
import pymongo, gateway_pb2, gateway_pb2_grpc, grpc, os
from utils import service_extended, save_chunks_to_file, serialize_to_buffer
from compile import HYCACHE, REGISTRY
import logger as l
import utils, iobigdata

from verify import get_service_hex_main_hash
from subprocess import check_output, CalledProcessError

WAIT_FOR_CONTAINER_DOWNLOAD = 10

def build_container_from_definition(service: gateway_pb2.celaut__pb2.Service):
    # Build the container from filesystem definition.
    pass

def get_container_from_outside(
    id: str,
    service_buffer: bytes,
    metadata: gateway_pb2.celaut__pb2.Any.Metadata
):
    # search container in a service. (docker-tar, docker-tar.gz, filesystem, ....)

    l.LOGGER('\nIt is not locally, ' + id + ' go to search the container in other node.')
    peers = list(pymongo.MongoClient(
                "mongodb://localhost:27017/"
            )["mongo"]["peerInstances"].find())

    for peer in peers:
        try:
            peer_uri = peer['uriSlot'][0]['uri'][0]
            l.LOGGER('\nUsing the peer ' + str(peer_uri) + ' for get the container of '+ id)
            
            #  Write the buffer to a file.
            save_chunks_to_file(
                filename = HYCACHE + id + '.tar',
                # chunks = search_container(service = service) TODO ??
                chunks = gateway_pb2_grpc.GatewayStub(  # Parse_from_buffer is not necesary because chunks're it.
                            grpc.insecure_channel(peer_uri['ip'] + ':' + str(peer_uri['port']))
                        ).GetServiceTar(
                            serialize_to_buffer(
                                service_extended(service = service_buffer, metadata = metadata),
                                indices=GetServiceTar_input_indices
                            )
                        )
            )
            
            l.LOGGER('    Buffer on file.')
            break
        except grpc.RpcError: # Other exception is raised.
            l.LOGGER('\nThe container with hash ' + id + ' is not in peer ' + str(peer_uri))
            continue
    l.LOGGER('Finded the container, go to build it.')

    #  Load the tar file to a docker container.
    try:
        os.system('docker load < ' + HYCACHE + id + '.tar')
        os.system('docker tag ' + id + ' ' + id + '.docker')
        check_output('/usr/bin/docker inspect ' + id, shell=True)
    except:
        l.LOGGER('Exception during load the tar ' + id + '.docker')
        raise Exception('Error building the container.')

    l.LOGGER('Build process finished ' + id)
    

def build(
    service_buffer: bytes,
    metadata: gateway_pb2.celaut__pb2.Any.Metadata,
    get_it_outside: bool = True
    ) -> str:
    id = get_service_hex_main_hash(service_buffer = service_buffer, metadata = metadata)
    l.LOGGER('\nBuilding ' + id)
    try:
        # it's locally?
        check_output('/usr/bin/docker inspect '+id+'.docker', shell=True)
        return id

    except CalledProcessError:
        if metadata.complete:
            second_partition_dir = REGISTRY + id + '/p2'
            with iobigdata.IOBigData().lock(len = len(service_buffer) + 2*os.path.getsize(second_partition_dir)):
                service = gateway_pb2.celaut_pb2.Service()
                service.ParseFromString(service_buffer)
                service.container.ParseFromString(
                    iobigdata.read_file(
                        filename = second_partition_dir
                    )
                )
                build_container_from_definition(
                    service = service
                )
        else:
            threading.Thread(
                target = get_container_from_outside,
                args = (id, service_buffer, metadata)          
                ).start() if get_it_outside else sleep(WAIT_FOR_CONTAINER_DOWNLOAD)
            raise Exception("Getting the container, the process will've time")

    # verify()

if __name__ == "__main__":
    import sys
    id = sys.argv[1]
    any = gateway_pb2.celaut__pb2.Any()
    any.ParseFromString(utils.read_file("/home/hy/node/__registry__/"+id))
    if get_service_hex_main_hash(service_buffer = any.value, metadata = any.metadata) == id:
        id = build(
            service_buffer = any.value, 
            metadata = any.metadata
            )
        print(id)
    else:
        l.LOGGER('Error: asignacion de servicio erronea en el registro.')
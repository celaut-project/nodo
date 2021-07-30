import pymongo, gateway_pb2, gateway_pb2_grpc, grpc, os
from utils import service_extended, save_chunks_to_file
from compile import HYCACHE
import logger as l

from verify import get_service_hash
from subprocess import check_output, CalledProcessError

def build(service: gateway_pb2.ipss__pb2.Service):
    id = get_service_hash(service=service, hash_type='sha3-256')
    l.LOGGER('\nBuilding ' + id)
    
    try:
        # it's locally?
        check_output('/usr/bin/docker inspect '+id+'.service', shell=True)
        
    except CalledProcessError:
        # search container in IPFS service. (docker-tar, docker-tar.gz, filesystem, ....)

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
                    chunks = gateway_pb2_grpc.GatewayStub(
                                grpc.insecure_channel(peer_uri['ip'] + ':' + str(peer_uri['port']))
                            ).GetServiceTar(
                                service_extended(service = service)
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
            os.system('docker tag ' + id + ' ' + id + '.service')
            check_output('/usr/bin/docker inspect ' + id + '.service', shell=True)
        except Exception as e:
            l.LOGGER('Exception during load the tar ' + id)
            raise Exception('Error building the container.')

        l.LOGGER('Build process finished ' + id)
    
    # verify()

if __name__ == "__main__":
    import sys
    id = sys.argv[1]
    with open("/home/hy/node/__registry__/"+id+".service", "rb") as file:
        service = gateway_pb2.ipss__pb2.Service()
        service.ParseFromString(file.read())
        if get_service_hash(service=service, hash_type='sha3-256') == id:
            build(service=service)
        else:
            l.LOGGER('Error: asignacion de servicio erronea en el registro.')
import random, pymongo, gateway_pb2, gateway_pb2_grpc, grpc, os
from utils import service_extended
from compile import LOGGER, HYCACHE
from verify import get_service_hash
from subprocess import check_output, CalledProcessError

def build(service: gateway_pb2.ipss__pb2.Service):
    id = get_service_hash(service=service, hash_type='sha3-256')
    LOGGER('\nBuilding ' + id)
    
    try:
        # it's locally?
        check_output('/usr/bin/docker inspect '+id+'.service', shell=True)
        
    except CalledProcessError:
        # search container in IPFS service. (docker-tar, docker-tar.gz, filesystem, ....)

        LOGGER('\nIt is not locally, ' + id + ' go to search the container in other node.')
        peers = pymongo.MongoClient(
            "mongodb://localhost:27017/"
        )["mongo"]["peerInstances"].find()

        for peer in peers:
            try:
                peer_uri = peer['uriSlot'][0]['uri'][0]
                LOGGER('\nUsing the peer ' + str(peer_uri) + ' for get the container of '+ id)
                
                #  Write the buffer to a file.
                open(
                    file=HYCACHE+id+'.tar',
                    mode='wb'
                ).write(
                    gateway_pb2_grpc.GatewayStub(
                        grpc.insecure_channel(peer_uri['uri'] + ':' + peer_uri['port'])
                    ).GetServiceTar(
                        service_extended(service=service)
                    ).buffer
                )
                break
            except grpc.RpcError:
                LOGGER('\nThe container with hash ' + id + ' is not in peer ' + peer_uri)
                continue
        LOGGER('Finded the container, go to build it.')

        #  Load the tar file to a docker container.
        try:
            os.system('docker load < ' + HYCACHE + id + '.tar')
            os.system('docker tag ' + id + ' ' + id + '.service')
            check_output('/usr/bin/docker inspect ' + id + '.service', shell=True)
        except Exception as e:
            LOGGER('Exception during load the tar ' + id)
            raise Exception('Error building the container.')

        LOGGER('Build process finished ' + id)
    
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
            LOGGER('Error: asignacion de servicio erronea en el registro.')
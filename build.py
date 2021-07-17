from compile import LOGGER
from verify import get_service_hash
from subprocess import check_output, CalledProcessError

def build(service):
    id = get_service_hash(service=service, hash_type='sha3-256')
    # it's locally?
    try:
        check_output('/usr/bin/docker inspect '+id+'.service', shell=True)
    except CalledProcessError:
        pass
        # search container in IPFS service. (docker-tar, docker-tar.gz, filesystem, ....)
    #verify()

if __name__ == "__main__":
    import gateway_pb2, sys
    id = sys.argv[1]
    with open("/home/hy/node/__registry__/"+id+".service", "rb") as file:
        service = gateway_pb2.ipss__pb2.Service()
        service.ParseFromString(file.read())
        if get_service_hash(service=service, hash_type='sha3-256') == id:
            build(service=service)
        else:
            LOGGER('Error: asignacion de servicio erronea en el registro.')
from compile import calculate_service_hash
from subprocess import check_output, CalledProcessError

import logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

def verify():
    pass

def build(service):
    id = calculate_service_hash(service.SerializeFromString(), "SHA3_256")
    # it's locally?
    try:
        check_output('/usr/bin/docker inspect '+id)
    except CalledProcessError:
        pass
        # search container in IPFS service. (docker-tar, docker-tar.gz, filesystem, ....)
    verify()

if __name__ == "__main__":
    import gateway_pb2, sys
    id = sys.argv[1]
    with open("/home/hy/node/__registry__/"+id+".service", "rb") as file:
        file = gateway_pb2.ServiceFile()
        file.ParseFromString(file.read())
        if calculate_service_hash(file.service, "SHA3_256") == id:
            build(file.service)
        else:
            print('Error: asignacion de servicio erronea en el registro.')
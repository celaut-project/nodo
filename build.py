from compile import SHA3_256, LOGGER
from subprocess import check_output, CalledProcessError


def verify():
    pass

def build(service):
    id = eval("SHA3_256")(service.SerializeFromString())
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
        if eval("SHA3_256")(file.service.SerializeToString()) == id:
            build(file.service)
        else:
            print('Error: asignacion de servicio erronea en el registro.')
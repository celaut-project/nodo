from time import sleep
import threading
from gateway_pb2_grpcbf import GetServiceTar_input
import pymongo, gateway_pb2, gateway_pb2_grpc, grpc, os, celaut_pb2, utils, iobigdata
from utils import service_extended
from grpcbigbuffer import save_chunks_to_file, serialize_to_buffer
from compile import HYCACHE, REGISTRY
import logger as l

from random import randint
from shutil import rmtree
from subprocess import check_output, run

from verify import get_service_hex_main_hash
from subprocess import check_output, CalledProcessError
from gateway import WAIT_FOR_CONTAINER

def build_container_from_definition(service: gateway_pb2.celaut__pb2.Service, metadata: gateway_pb2.celaut__pb2.Any.Metadata, id: str):
    l.LOGGER('\nIt is not locally, ' + id + ' go to build the container.')
    # Build the container from filesystem definition.
    def write_item(b: celaut_pb2.Service.Container.Filesystem.ItemBranch, dir: str, symlinks):
        if b.HasField('filesystem'):
            os.mkdir(dir + b.name)
            write_fs(fs = b.filesystem, dir = dir + b.name + '/', symlinks = symlinks)

        elif b.HasField('file'):
            open(dir + b.name, 'wb').write(
                b.file
            )
            
        else:
            symlinks.append(b.link)

    def write_fs(fs: celaut_pb2.Service.Container.Filesystem, dir: str, symlinks):
        for branch in fs.branch:
            write_item(
                b = branch,
                dir = dir,
                symlinks = symlinks
            )

    # Take filesystem.
    fs = celaut_pb2.Service.Container.Filesystem()
    fs.ParseFromString(
        service.container.filesystem
    )

    # Take architecture.
    arch = 'linux/arm64' # get_arch_tag(service_with_meta=service_with_meta) # TODO: get_arch_tag, selecciona el tag de la arquitectura definida por el servicio, en base a la especificacion y metadatos, que tiene el nodo para esa arquitectura.
    
    try:
        os.mkdir('__hycache__')
    except: pass

    # Write all on hycache.
    id = str(randint(1,999))
    dir = '__hycache__/builder'+id
    os.mkdir(dir)
    fs_dir = dir + '/fs'
    os.mkdir(fs_dir)
    symlinks = []
    l.LOGGER('Build process of '+ id + ': writting filesystem.')
    write_fs(fs = fs, dir = fs_dir + '/', symlinks = symlinks)

    # Build it.
    l.LOGGER('Build process of '+ id + ': docker build it.')
    open(dir+'/Dockerfile', 'w').write('FROM scratch\nCOPY fs .\nENTRYPOINT /random/start.py')
    check_output('docker buildx build --platform '+arch+' -t '+id+' '+dir+'/.', shell=True)
    try:
        rmtree(dir)
    except Exception: pass

    # Generate the symlinks.
    overlay_dir = check_output("docker inspect --format='{{ .GraphDriver.Data.UpperDir }}' "+id, shell=True).decode('utf-8')[:-1]
    for symlink in symlinks: run('ln -s '+symlink.src+' '+symlink.dst[1:], shell=True, cwd=overlay_dir)

    # Apply permissions. # TODO check that is only own by the container root. https://programmer.ink/think/docker-security-container-resource-control-using-cgroups-mechanism.html
    run('find . -type d -exec chmod 777 {} \;', shell=True, cwd=overlay_dir)
    run('find . -type f -exec chmod 777 {} \;', shell=True, cwd=overlay_dir)

    l.LOGGER('Build process of '+ id + ': finished.')


def get_container_from_outside( # TODO could take it from a specific ledger.
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
                                indices=GetServiceTar_input
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

    l.LOGGER('Outside load container process finished ' + id)
    

def build(
        service_buffer: bytes,
        metadata: gateway_pb2.celaut__pb2.Any.Metadata,
        get_it: bool = True,
        id = None,
    ) -> str:
    if not id: id = get_service_hex_main_hash( metadata = metadata)
    if get_it: l.LOGGER('\nBuilding ' + id)
    try:
        # check if it's locally.
        check_output('/usr/bin/docker inspect '+id+'.docker', shell=True)
        return id

    except CalledProcessError:
        if metadata.complete:
            if get_it:
                second_partition_dir = REGISTRY + id + '/p2'
                with iobigdata.mem_manager(len = len(service_buffer) + 2*os.path.getsize(second_partition_dir)):
                    service = gateway_pb2.celaut_pb2.Service()
                    service.ParseFromString(service_buffer)
                    service.container.ParseFromString(
                        iobigdata.read_file(
                            filename = second_partition_dir
                        )
                    )
                    threading.Thread(
                            target = build_container_from_definition,
                            args = (
                                service,
                                metadata,
                                id
                            )
                        ).start()
            else: sleep( WAIT_FOR_CONTAINER )
            raise Exception("Getting the container, the process will've time")
        else:
            threading.Thread(
                    target = get_container_from_outside,
                    args = (id, service_buffer, metadata)          
                ).start() if get_it else sleep( WAIT_FOR_CONTAINER )
            raise Exception("Getting the container, the process will've time")

    # verify() TODO ??

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
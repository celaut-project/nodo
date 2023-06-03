import json
from time import sleep, time
import threading
from typing import Tuple

from protos.gateway_pb2_grpcbf import GetServiceTar_input
import grpc, os, src.manager.resources_manager as resources_manager
from protos import celaut_pb2, gateway_pb2, gateway_pb2_grpc
from src.utils.env import DOCKER_COMMAND, SUPPORTED_ARCHITECTURES, BUILD_CONTAINER_MEMORY_SIZE_FACTOR, \
    WAIT_FOR_CONTAINER, BLOCKDIR, CACHE, REGISTRY
from src.utils.utils import generate_uris_by_peer_id, peers_id_iterator, service_extended, read_file
import src.utils.logger as l
from grpcbigbuffer.client import save_chunks_to_file, serialize_to_buffer, copy_block_if_exists

from shutil import rmtree
from subprocess import run
import itertools

from src.utils.verify import get_service_hex_main_hash
from subprocess import check_output, CalledProcessError


def get_arch_tag(metadata: celaut_pb2.Any.Metadata) -> str:
    for _l in SUPPORTED_ARCHITECTURES:
        if any(a in _l for a in {
            ah.key: ah.value for ah in {
                ah.key: ah.value for ah in
                metadata.hashtag.attr_hashtag
            }[1][0].attr_hashtag
        }[1][0].tag):
            return _l[0]


def check_supported_architecture(metadata: celaut_pb2.Any.Metadata) -> bool:
    try:
        return any(a in list(itertools.chain.from_iterable(SUPPORTED_ARCHITECTURES)) for a in
                   {ah.key: ah.value for ah in
                    {ah.key: ah.value for ah in metadata.hashtag.attr_hashtag}[1][0].attr_hashtag}[1][0].tag)
    except Exception:
        return False


class WaitBuildException(Exception):

    def __init__(self, *args):
        self.message = None

    def __str__(self):
        return "Getting the container, the process will've time"


class UnsupportedArchitectureException(Exception):

    def __init__(self, arch):
        self.message = f"\n Unsupported architecture {arch} - \n Only these {SUPPORTED_ARCHITECTURES}. \n"

    def __str__(self):
        return self.message


actual_building_processes_lock = threading.Lock()
actual_building_processes = []  # list of hexadecimal string sha256 value hashes.


def build_container_from_definition(service: celaut_pb2.Service,
                                    metadata: gateway_pb2.celaut__pb2.Any.Metadata,
                                    service_id: str):
    # Build the container from filesystem definition.
    def write_item(b: celaut_pb2.Service.Container.Filesystem.ItemBranch, dir_element: str, symlinks_element):
        if b.HasField('filesystem'):
            os.mkdir(dir_element + b.name)
            write_fs(fs_element=b.filesystem, dir_element=dir_element + b.name + '/', symlinks_element=symlinks_element)

        elif b.HasField('file'):
            if not copy_block_if_exists(
                    buffer=b.file,
                    directory=dir_element + b.name
            ):
                open(dir_element + b.name, 'wb').write(
                    b.file
                )

        else:
            symlinks_element.append(b.link)

    def write_fs(fs_element: celaut_pb2.Service.Container.Filesystem, dir_element: str, symlinks_element):
        for branch in fs_element.branch:
            write_item(
                b=branch,
                dir_element=dir_element,
                symlinks_element=symlinks_element
            )

    if not check_supported_architecture(metadata=metadata):
        l.LOGGER('Build process of ' + service_id + ': unsupported architecture.')
        raise UnsupportedArchitectureException(arch=str(metadata))

    l.LOGGER('Build process of ' + service_id + ': wait for unlock the memory.')

    # Get the size of the service's biggest block.
    with open(f"{REGISTRY}{get_service_hex_main_hash(metadata=metadata)}/_.json", 'r') as f:
        try:
            biggest_block_size: int = max([os.path.getsize(BLOCKDIR + c[0])
                                           for c in json.load(f) if type(c) is Tuple])
        except ValueError:
            biggest_block_size: int = 0

    with resources_manager.mem_manager(
            len=sum([
                service.ByteSize(),
                biggest_block_size
            ]) * BUILD_CONTAINER_MEMORY_SIZE_FACTOR
    ):
        # TODO si el coste es mayor a la cantidad total se quedará esperando indefinidamente.
        l.LOGGER('Build process of ' + service_id + ': go to load all the buffer.')
        l.LOGGER('Build process of ' + service_id + ': filesystem load in memory.')

        # Take filesystem.
        fs = celaut_pb2.Service.Container.Filesystem()
        fs.ParseFromString(
            service.container.filesystem
        )

        # Take architecture.
        arch = get_arch_tag(metadata=metadata)
        # get_arch_tag, selecciona el tag de la arquitectura definida por el servicio,
        #  en base a la especificación y metadatos, que tiene el nodo para esa arquitectura.
        l.LOGGER('Build process of ' + service_id + ': select the architecture ' + str(arch))

        try:
            os.mkdir(CACHE)
        except:
            pass

        # Write all on cache.
        dir = CACHE + 'builder' + service_id
        os.mkdir(dir)
        fs_dir = dir + '/fs'
        os.mkdir(fs_dir)
        symlinks = []
        l.LOGGER('Build process of ' + service_id + ': writting filesystem.')
        write_fs(fs_element=fs, dir_element=fs_dir + '/', symlinks_element=symlinks)

        # Build it.
        l.LOGGER('Build process of ' + service_id + ': docker building it ...')
        open(dir + '/Dockerfile', 'w').write('FROM scratch\nCOPY fs .')
        cache_id = service_id + str(time()) + '.cache'
        check_output('docker buildx build --platform ' + arch + ' -t ' + cache_id + ' ' + dir + '/.', shell=True)
        l.LOGGER('Build process of ' + service_id + ': docker build it.')
        try:
            rmtree(dir)
        except Exception:
            pass

        # Generate the symlinks.
        overlay_dir = check_output("docker inspect --format='{{ .GraphDriver.Data.UpperDir }}' " + cache_id,
                                   shell=True).decode('utf-8')[:-1]
        l.LOGGER('Build process of ' + service_id + ': overlay dir ' + str(overlay_dir))
        for symlink in symlinks:
            try:
                if check_output('ln -s ' + symlink.src + ' ' + symlink.dst[1:], shell=True, cwd=overlay_dir)[
                   :2] == 'ln': break
            except CalledProcessError:
                l.LOGGER(
                    'Build process of ' + service_id + ': symlink error (CalledProcessError) ' + str(symlink.src) + str(
                        symlink.dst))
                break
            except AttributeError:
                l.LOGGER(
                    'Build process of ' + service_id + ': symlink error (AttributeError) ' + str(symlink.src) + str(
                        symlink.dst))

        l.LOGGER('Build process of ' + service_id + ': apply permissions.')
        # Apply permissions. # TODO check that is only own by the container root.
        #  https://programmer.ink/think/docker-security-container-resource-control-using-cgroups-mechanism.html
        run('find . -type d -exec chmod 777 {} \;', shell=True, cwd=overlay_dir)
        run('find . -type f -exec chmod 777 {} \;', shell=True, cwd=overlay_dir)

        check_output('docker image tag ' + cache_id + ' ' + service_id + '.docker', shell=True)
        check_output('docker rmi ' + cache_id, shell=True)
        l.LOGGER('Build process of ' + service_id + ': finished.')

        actual_building_processes_lock.acquire()
        actual_building_processes.remove(service_id)
        actual_building_processes_lock.release()


def get_container_from_outside(  # TODO could take it from a specific ledger.
        id: str,
        metadata: gateway_pb2.celaut__pb2.Any.Metadata
):
    # search container in a service. (docker-tar, docker-tar.gz, filesystem, ....)

    l.LOGGER('\nIt is not locally, ' + id + ' go to search the container in other node.')
    for peer in peers_id_iterator():
        try:
            l.LOGGER('\nUsing the peer ' + peer + ' for get the container of ' + id)

            #  Write the buffer to a file.
            save_chunks_to_file(
                filename=CACHE + id + '.tar',
                # chunks = search_container(service = service) TODO ??
                buffer_iterator=gateway_pb2_grpc.GatewayStub(  # Parse_from_buffer is not necesary because chunks're it.
                    grpc.insecure_channel(
                        next(generate_uris_by_peer_id(peer)),
                    )
                ).GetServiceTar(
                    serialize_to_buffer(
                        service_extended(metadata=metadata),
                        indices=GetServiceTar_input
                    )
                )
            )

            l.LOGGER('    Buffer on file.')
            break
        except grpc.RpcError:  # Other exception is raised.
            l.LOGGER('\nThe container with hash ' + id + ' is not in peer ' + peer)
            continue
    l.LOGGER('Finded the container, go to build it.')

    #  Load the tar file to a docker container.
    try:
        os.system('docker load < ' + CACHE + id + '.tar')
        os.system('docker tag ' + id + ' ' + id + '.docker')
        check_output(DOCKER_COMMAND + ' inspect ' + id, shell=True)
    except:
        l.LOGGER('Exception during load the tar ' + id + '.docker')
        raise Exception('Error building the container.')

    l.LOGGER('Outside load container process finished ' + id)

    actual_building_processes_lock.acquire()
    actual_building_processes.remove(id)
    actual_building_processes_lock.release()


def build(
        service: celaut_pb2.Service,
        metadata: gateway_pb2.celaut__pb2.Any.Metadata,
        get_it: bool = True,
        service_id=None,
        complete=False
) -> str:
    if not service_id:
        try:
            service_id = get_service_hex_main_hash(
                metadata=metadata
            )
        except Exception as e:
            l.LOGGER("Builder exception: can't obtain the service identifier")
            raise e

    if get_it: l.LOGGER('Building ' + service_id)
    try:
        # check if it's locally.
        check_output(DOCKER_COMMAND + ' inspect ' + service_id + '.docker', shell=True)
        return service_id

    except CalledProcessError:
        if complete:
            sleep(1)  # TODO -> TMB(typical multithread bug).
            if get_it and service_id not in actual_building_processes:
                actual_building_processes_lock.acquire()
                actual_building_processes.append(service_id)
                actual_building_processes_lock.release()

                threading.Thread(
                    target=build_container_from_definition,
                    args=(
                        service,
                        metadata,
                        service_id
                    )
                ).start()
            else:
                sleep(WAIT_FOR_CONTAINER)
            raise WaitBuildException
        else:
            if service_id not in actual_building_processes:
                actual_building_processes_lock.acquire()
                actual_building_processes.append(service_id)
                actual_building_processes_lock.release()
                threading.Thread(
                    target=get_container_from_outside,
                    args=(service_id, metadata)
                ).start() if get_it else sleep(WAIT_FOR_CONTAINER)
                raise WaitBuildException

    # verify() TODO ??

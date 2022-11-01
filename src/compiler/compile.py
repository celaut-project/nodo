import string
from math import ceil

from typing import Generator, List

from src.utils import logger as l
import sys, shutil
import json
import os, subprocess
import src.manager.resources_manager as resources_manager
import grpcbigbuffer
from protos import buffer_pb2, celaut_pb2 as celaut, service_capnp, gateway_pb2
from src.utils.env import COMPILER_SUPPORTED_ARCHITECTURES, HYCACHE, COMPILER_MEMORY_SIZE_FACTOR, SAVE_ALL, REGISTRY, \
    BLOB_SIZE
from src.utils.utils import get_service_hex_main_hash
from src.utils.verify import get_service_list_of_hashes, calculate_hashes

class Hyper:
    def __init__(self, path, aux_id):
        super().__init__()
        self.service = service_capnp.Service()
        self.metadata = celaut.Any.Metadata()
        self.path = path
        self.json = json.load(open(self.path+"service.json", "r"))
        self.aux_id = aux_id

        arch = None
        for a in COMPILER_SUPPORTED_ARCHITECTURES:
            if self.json.get('architecture') in a: arch = a[0]

        if not arch: raise Exception("Can't compile this service, not supported architecture.")

        # Directories are created on cache.
        os.system("mkdir "+HYCACHE+self.aux_id+"/building")
        os.system("mkdir "+HYCACHE+self.aux_id+"/filesystem")

        # Build container and get compressed layers.
        if not os.path.isfile(self.path+'Dockerfile'): raise Exception("Error: Dockerfile no encontrado.")
        os.system('/usr/bin/docker buildx build --platform '+ arch +' --no-cache -t builder'+self.aux_id+' '+self.path)
        os.system("/usr/bin/docker save builder"+self.aux_id+" > "+HYCACHE+self.aux_id+"/building/container.tar")
        os.system("tar -xvf "+HYCACHE+self.aux_id+"/building/container.tar -C "+HYCACHE+self.aux_id+"/building/")

        self.buffer_len = int(subprocess.check_output(["/usr/bin/docker image inspect builder"+aux_id+" --format='{{.Size}}'"], shell=True))


    def parseContainer(self):
        def parseFilesys() -> celaut.Any.Metadata.HashTag:
            # Save his filesystem on cache.
            for layer in os.listdir(HYCACHE+self.aux_id+"/building/"):
                if os.path.isdir(HYCACHE+self.aux_id+"/building/"+layer):
                    l.LOGGER('Unzipping layer '+layer)
                    os.system("tar -xvf "+HYCACHE+self.aux_id+"/building/"+layer+"/layer.tar -C "+HYCACHE+self.aux_id+"/filesystem/")

            # Add filesystem data to filesystem buffer object.
            def recursive_parsing(directory: str) -> service_capnp.Filesystem:
                host_dir = HYCACHE+self.aux_id+"/filesystem"
                filesystem = service_capnp.Filesystem()
                dir_list = os.listdir(host_dir + directory)
                branches = filesystem.init('branch', len(dir_list))
                for i in range(len(dir_list)):
                    b_name = dir_list[i]
                    if b_name == '.wh..wh..opq':
                        # https://github.com/opencontainers/image-spec/blob/master/layer.md#opaque-whiteout
                        continue
                    branch = service_capnp.Filesystem.ItemBranch()
                    branch.name = os.path.basename(b_name)

                    # It's a link.
                    if os.path.islink(host_dir + directory+b_name):
                        link = branch.item.init('link')
                        link.dst = directory+b_name
                        link.src = os.path.realpath(host_dir+directory+b_name)[len(host_dir):] if host_dir in os.path.realpath(host_dir+directory+b_name) else os.path.realpath(host_dir+directory+b_name)

                    # It's a file.
                    elif os.path.isfile(host_dir + directory+b_name):
                        file_dir: str = host_dir + directory+b_name
                        file = branch.item.init(
                            'file',
                            ceil(os.path.getsize(file_dir) / BLOB_SIZE)  # Number of chunks of 512 MB
                        )
                        with open(host_dir + directory+b_name, 'rb', buffering = BLOB_SIZE) as f:
                            chunk_i: int = 0
                            while True:
                                f.flush()
                                chunk = f.read(BLOB_SIZE)
                                if len(chunk) == 0: break
                                file[chunk_i] = chunk; chunk_i += 1

                    # It's a folder.
                    elif os.path.isdir(host_dir + directory+b_name):
                        branch.item.filesystem = recursive_parsing(directory = directory+b_name+'/')

                    branches[i] = branch

                return filesystem

            filesystem: service_capnp.Filesystem = recursive_parsing( directory = "/" )
            self.service.container.filesystem = filesystem

            return celaut.Any.Metadata.HashTag(
                hash = calculate_hashes( value = filesystem.to_bytes() )
            )


        # Envs
        if False and self.json.get('envs'):
            for env in self.json.get('envs'):
                try:
                    with open(self.path+env+".field", "rb") as env_desc:
                        self.service.container.enviroment_variables[env].ParseFromString(env_desc.read())
                except FileNotFoundError:
                    pass

        # Entrypoint
        if self.json.get('entrypoint'):
            entrypoint_list: List[str] = self.json.get('entrypoint')
            entrypoint_length: int = len(entrypoint_list)
            entrypoint = self.service.container.init('entrypoint', entrypoint_length)
            for i in range(entrypoint_length):
                entrypoint[i] = entrypoint_list[i]
                

        # Arch
        
        
        # Config file spec.
        configuration = celaut.Service.Container.Config()
        configuration.path.append('__config__')
        configuration.format.CopyFrom(
            celaut.FieldDef() # celaut.ConfigFile definition.
        )
        self.service.container.configuration = configuration.SerializeToString()

        # Expected Gateway.

        
        # Add container metadata to the global metadata.
        self.metadata.hashtag.attr_hashtag.append(
            celaut.Any.Metadata.HashTag.AttrHashTag(
                key = 1,  # Container attr.
                value = [
                    celaut.Any.Metadata.HashTag(
                        attr_hashtag = [
                            celaut.Any.Metadata.HashTag.AttrHashTag(
                                key = 1,  # Architecture
                                value = [ 
                                    celaut.Any.Metadata.HashTag(
                                        tag = [
                                            self.json.get('architecture')
                                        ]
                                    ) 
                                ]
                            ),
                            celaut.Any.Metadata.HashTag.AttrHashTag(
                                key = 2,  # Filesystem
                                value = [ parseFilesys() ]
                            )
                        ]
                    )
                ]
            )
        )


    def parseApi(self):
        #  App protocol
        try:
            with open(self.path + "api.application", "rb") as api_desc:
                self.service.api.app_protocol.ParseFromString(api_desc.read())
        except:
            pass

        #  Slots 
        if not self.json.get('api'): return
        for item in self.json.get('api'): #  iterate slots.
            slot = celaut.Service.Api.Slot()
            # port.
            slot.port = item.get('port')                
            #  transport protocol.
            #  TODO: slot.transport_protocol = Protocol()
            self.service.api.slot.append(slot)

        # Add api metadata to the global metadata.
        self.metadata.hashtag.attr_hashtag.append(
            celaut.Any.Metadata.HashTag.AttrHashTag(
                key = 2,  # Api attr.
                value = [ 
                    celaut.Any.Metadata.HashTag(
                        attr_hashtag = [
                            celaut.Any.Metadata.HashTag.AttrHashTag(
                                key = 2,  # Slot attr.
                                value = [ 
                                    celaut.Any.Metadata.HashTag(
                                        attr_hashtag = [
                                            celaut.Any.Metadata.HashTag.AttrHashTag(
                                                key = 2,  # Transport Protocol attr.
                                                value = [
                                                    celaut.Any.Metadata.HashTag(
                                                        tag = item.get('protocol')
                                                    )
                                                ]
                                            )
                                        ]
                                    ) for item in self.json.get('api')
                                ]
                            )
                        ]
                    )
                 ]
            )
        )

    def parseLedger(self):
        #  TODO: self.service.ledger.

        #  Add ledger metadata to the global metadata.
        if self.json.get('ledger'):
            self.metadata.hashtag.attr_hashtag.append(
                celaut.Any.Metadata.HashTag.AttrHashTag(
                    key = 4,  # Ledger attr.
                    value = (
                        celaut.Any.MetaData.HashTag(
                            tag = self.json.get('ledger')
                        )
                    )
                )
            )
            

    def save(self, partitions_model: tuple) -> str:
        service_buffer = self.service.to_bytes() # 2*len TODO CHECK -> capn proto duplicates the buffer too?
        self.metadata.hashtag.hash.extend(
            get_service_list_of_hashes(
                service_buffer = service_buffer, 
                metadata = self.metadata
            )
        )
        id = get_service_hex_main_hash(
            service_buffer = service_buffer,  
            metadata = self.metadata
            )

        # Once service hashes are calculated, we prune the filesystem for save storage.
        #self.service.container.filesystem.ClearField('branch')
        # https://github.com/moby/moby/issues/20972#issuecomment-193381422
        del service_buffer # -len
        os.mkdir(HYCACHE + 'compile' + id + '/')


        l.LOGGER('Compiler: DIR CREATED'+ HYCACHE + 'compile' + id + '/')

        try:
            if len(partitions_model) == 1:
                service_capnp.CompileOutput(
                    id=bytes.fromhex(id),
                    service=service_capnp.ServiceWithMeta(
                        metadata=self.metadata.SerializeToString(),
                        service=self.service
                    )
                ).write(
                    open(HYCACHE + 'compile' + id + '/p' + str(1), 'w+b')
                )
            else:
                raise Exception('gRPCbb dont support partition conversion for capnp objects.')
                for i, partition in enumerate(partitions_model):
                    message = grpcbigbuffer.get_submessage(
                                    partition = partition,
                                    obj = gateway_pb2.CompileOutput(
                                        id = bytes.fromhex(id),
                                        service = service_capnp.ServiceWithMeta(
                                                metadata = self.metadata.SerializeToString(),
                                                service = self.service
                                            ).to_bytes()
                                    )
                                )
                    message_buffer = grpcbigbuffer.message_to_bytes(
                                message = message
                            )
                    l.LOGGER('Compiler: send message '+ str(type(message)) + ' ' + str(partition) + ' ' + str(len(message_buffer)))
                    with open(HYCACHE + 'compile' + id + '/p'+str(i+1), 'wb') as f:
                        f.write(
                            message_buffer
                        )
        except Exception as e:
            l.LOGGER('E -> '+str(e))
        return id


def ok( path, aux_id,
        partitions_model = (buffer_pb2.Buffer.Head.Partition())
    ):
    spec_file = Hyper(path = path, aux_id = aux_id)

    with resources_manager.mem_manager(len = COMPILER_MEMORY_SIZE_FACTOR*spec_file.buffer_len):
        spec_file.parseContainer()
        # spec_file.parseApi()
        # spec_file.parseLedger()

        identifier = spec_file.save(
            partitions_model=partitions_model
            )

    os.system('/usr/bin/docker tag builder' + aux_id +' ' + identifier + '.docker')
    os.system('/usr/bin/docker rmi builder'+aux_id)
    os.system('rm -rf '+HYCACHE+aux_id+'/')
    return identifier

def repo_ok(
    repo: str,
    partitions_model: list
) -> str:
    import random
    aux_id = str(random.random())
    git = str(repo)
    git_repo = git.split('::')[0]
    branch = git.split('::')[1]
    os.system('git clone --branch '+branch+' '+git_repo+' '+HYCACHE+aux_id+'/for_build/git')
    return ok(
        path = HYCACHE+aux_id+'/for_build/git/.service/',
        aux_id = aux_id,
        # partitions_model=partitions_model
        )  # Hyperfile


def zipfile_ok(
    repo: str,
    partitions_model: list
) -> str:
    import random
    aux_id = str(random.random())
    os.system('mkdir '+HYCACHE+aux_id)
    os.system('mkdir '+HYCACHE+aux_id+'/for_build')
    os.system('unzip '+repo+' -d '+HYCACHE+aux_id+'/for_build')
    os.system('rm '+repo)
    return ok(
        path = HYCACHE+aux_id+'/for_build/',
        aux_id = aux_id,
        partitions_model=partitions_model
        )  # Hyperfile

def compile(repo, partitions_model: list, saveit: bool = SAVE_ALL) -> Generator[buffer_pb2.Buffer, None, None]:
    l.LOGGER('Compiling zip ' + str(repo))
    id = zipfile_ok(
        repo = repo,
        partitions_model = list(partitions_model)
    )
    """
    dirs = sorted([d for d in os.listdir(HYCACHE+'compile'+id)])
    for b in grpcbigbuffer.serialize_to_buffer(
        message_iterator =tuple([service_capnp.CompileOutput]) + tuple([grpcbigbuffer.Dir(dir=HYCACHE + 'compile' + id + '/' + d) for d in dirs]),
        # partitions_model = list(partitions_model),
        indices = service_capnp.CompileOutput
    ): yield b
    shutil.rmtree(HYCACHE+'compile'+id)    
    """

    # TODO if saveit: convert dirs to local partition model and save it into the registry.


if __name__ == "__main__":
    from protos.gateway_pb2_grpcbf import StartService_input_partitions_v2
    id = repo_ok(
        repo = sys.argv[1],
        partitions_model = StartService_input_partitions_v2[2] if not len(sys.argv) > 1 else [
            buffer_pb2.Buffer.Head.Partition()]
    )
    os.system('mv '+HYCACHE+'compile'+id+' '+REGISTRY+id)
    print(id)

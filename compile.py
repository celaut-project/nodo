from typing import Generator
import logger as l
import sys, shutil
import json, getpass
import os, subprocess
import iobigdata
import celaut_pb2 as celaut, grpcbigbuffer, buffer_pb2, gateway_pb2, compile_pb2
from verify import get_service_list_of_hashes, calculate_hashes, get_service_hex_main_hash

#  -------------------------------------------------
#  -------------------------------------------------
#  DOCKERFILE AND JSON   to   PROTOBUF SERVICE SPEC.
#  -------------------------------------------------
#  -------------------------------------------------

# DIRECTORIES
USER_NAME = getpass.getuser()
HYCACHE = "/home/"+USER_NAME+"/node/__hycache__/"
REGISTRY = "/home/"+USER_NAME+"/node/__registry__/"
SAVE_ALL = False

class Hyper:
    def __init__(self, path, aux_id):
        super().__init__()
        self.service =  compile_pb2.Service()
        self.metadata = celaut.Any.Metadata(
            complete = True
        )
        self.path = path
        self.json = json.load(open(self.path+"service.json", "r"))
        self.aux_id = aux_id

        # Directories are created on cache.
        os.system("mkdir "+HYCACHE+self.aux_id+"/building")
        os.system("mkdir "+HYCACHE+self.aux_id+"/filesystem")

        # Build container and get compressed layers.
        if not os.path.isfile(self.path+'Dockerfile'): raise Exception("Error: Dockerfile no encontrado.")
        os.system('/usr/bin/docker build -t builder'+self.aux_id+' '+self.path)
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
            def recursive_parsing(directory: str) -> celaut.Service.Container.Filesystem:
                filesystem = celaut.Service.Container.Filesystem()
                for b_name in os.listdir(directory):
                    if b_name == '.wh..wh..opq':
                        # https://github.com/opencontainers/image-spec/blob/master/layer.md#opaque-whiteout
                        l.LOGGER('docker opaque witeout file.')
                        continue
                    branch = celaut.Service.Container.Filesystem.ItemBranch()
                    branch.name = os.path.basename(b_name)

                    # It's a link.
                    if os.path.islink(directory+b_name):
                        l.LOGGER('    Adding link '+ b_name)
                        branch.link = os.path.realpath(directory+b_name)

                    # It's a file.
                    elif os.path.isfile(directory+b_name):
                        l.LOGGER('    Adding file '+ b_name)
                        with open(directory+b_name, 'rb') as file:
                            branch.file = file.read()

                    # It's a folder.
                    elif os.path.isdir(directory+b_name):
                        l.LOGGER('    Adding directory '+ b_name)
                        branch.filesystem.CopyFrom(
                            recursive_parsing(directory=directory+b_name+'/')
                            )

                    filesystem.branch.append(branch)

                return filesystem

            self.service.container.filesystem.CopyFrom(recursive_parsing( directory = HYCACHE+self.aux_id+"/filesystem/" ))

            return celaut.Any.Metadata.HashTag(
                hash = calculate_hashes( value = self.service.container.filesystem.SerializeToString() )
            )


        # Envs
        if self.json.get('envs'):
            for env in self.json.get('envs'):
                try:
                    with open(self.path+env+".field", "rb") as env_desc:
                        self.service.container.enviroment_variables[env].ParseFromString(env_desc.read())
                except FileNotFoundError:
                    self.service.container.enviroment_variables[env]        

        # Entrypoint
        if self.json.get('entrypoint'):
            self.service.container.entrypoint = self.json.get('entrypoint')

        # Arch
        
        
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
            self.service.api.config.path.append('__config__')
            self.service.api.config.format.CopyFrom(
                celaut.FieldDef() # celaut.ConfigFile definition.
            )

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

    def parseTensor(self):
        tensor = self.json.get('tensor') or None
        if tensor:
            self.service.tensor.rank = tensor["rank"] or None
            indexes = tensor.get('index') or None
            if indexes:
                for var in indexes:
                    try:
                        with open(self.path+var+".field", "rb") as var_desc:
                            self.service.tensor.index[var].ParseFromString(var_desc.read())
                    except FileNotFoundError: pass
        
                # Add tensor metadata to the global metadata.
                self.metadata.hashtag.attr_hashtag.append(
                    celaut.Any.Metadata.HashTag.AttrHashTag(
                        key = 3,  # Tensor attr.
                        value = [
                            celaut.Any.Metadata.HashTag(
                                attr_hashtag = [
                                    celaut.Any.Metadata.HashTag.AttrHashTag(
                                        key = 1,  # index attr.
                                        value = [
                                            celaut.Any.Metadata.HashTag(
                                                tag = indexes[var]
                                            ) for var in indexes
                                        ]
                                    )
                                ]
                            ),
                        ]
                    )
                )

    def save(self, partitions_model: list) -> str:
        self.metadata.complete = True
        service_buffer = self.service.SerializeToString() # 2*len
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


        print('DIR CREATED', HYCACHE + 'compile' + id + '/')

        for i, partition in enumerate(partitions_model):
            with open(HYCACHE + 'compile' + id + '/p'+str(i+1), 'wb') as f:
                f.write(
                    grpcbigbuffer.message_to_bytes(
                        message = grpcbigbuffer.get_submessage(
                            partition = partition, 
                            obj = gateway_pb2.CompileOutput(
                                id = bytes.fromhex(id),
                                service = compile_pb2.ServiceWithMeta(
                                        metadata = self.metadata,
                                        service = self.service
                                    )
                            )
                        )
                    )
                )
        return id


def ok(path, aux_id, partitions_model = [buffer_pb2.Buffer.Head.Partition()]):
    Hyperfile = Hyper(path = path, aux_id = aux_id)

    print(Hyperfile.buffer_len)
    with iobigdata.mem_manager(len = 2*Hyperfile.buffer_len):
        Hyperfile.parseContainer()
        Hyperfile.parseApi()
        Hyperfile.parseLedger()
        Hyperfile.parseTensor()

        id = Hyperfile.save(
            partitions_model=partitions_model
            )
    return id

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
    l.LOGGER(str(os.listdir(HYCACHE+aux_id+'/for_build/git/.service/')))
    id = ok(
        path = HYCACHE+aux_id+'/for_build/git/.service/',
        aux_id = aux_id,
        partitions_model=partitions_model
        )  # Hyperfile

    os.system('/usr/bin/docker tag builder'+aux_id+' '+id+'.docker')
    os.system('/usr/bin/docker rmi builder'+aux_id)
    os.system('rm -rf '+HYCACHE+aux_id+'/')
    return id



def compile(repo, partitions_model: list, saveit: bool = SAVE_ALL) -> Generator[buffer_pb2.Buffer, None, None]:
    id = repo_ok(
        repo = repo,
        partitions_model = list(partitions_model)
    )
    print([grpcbigbuffer.Dir(dir=HYCACHE+'compile'+id+'/'+d)for d in os.listdir(HYCACHE+'compile'+id)])
    for b in grpcbigbuffer.serialize_to_buffer(
        message_iterator = tuple([gateway_pb2.CompileOutput])+tuple([grpcbigbuffer.Dir(dir=HYCACHE+'compile'+id+'/'+d)for d in os.listdir(HYCACHE+'compile'+id)]),
        partitions_model = list(partitions_model),
        indices = gateway_pb2.CompileOutput
    ): yield b
    shutil.rmtree(HYCACHE+'compile'+id)
    # TODO if saveit: convert dirs to local partition model and save it into the registry.


if __name__ == "__main__":
    from gateway_pb2_grpcbf import StartService_input_partitions_v2
    id = repo_ok(
        repo = sys.argv[1],
        partitions_model = StartService_input_partitions_v2[2] if not sys.argv[2] else [buffer_pb2.Buffer.Head.Partition()]
    )
    os.system('mv '+HYCACHE+'compile'+id+' '+REGISTRY+id)
    print(id)

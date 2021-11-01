import logger as l
import sys
import json
import os
import celaut_pb2 as celaut
from verify import get_service_list_of_hashes, calculate_hashes, get_service_hex_main_hash

#  -------------------------------------------------
#  -------------------------------------------------
#  DOCKERFILE AND JSON   to   PROTOBUF SERVICE SPEC.
#  -------------------------------------------------
#  -------------------------------------------------

# DIRECTORIES
HYCACHE = "/home/hy/node/__hycache__/"
REGISTRY = "/home/hy/node/__registry__/"

class Hyper:
    def __init__(self, path, aux_id):
        super().__init__()
        self.service =  celaut.Service()
        self.metadata = celaut.Any.Metadata(
            complete = True
        )
        self.path = path
        self.json = json.load(open(self.path+"service.json", "r"))
        self.aux_id = aux_id


    def parseContainer(self):
        def parseFilesys() -> celaut.Any.Metadata.HashTag:
            # Directories are created on cache.
            os.system("mkdir "+HYCACHE+self.aux_id+"/building")
            os.system("mkdir "+HYCACHE+self.aux_id+"/filesystem")

            # Build container and get compressed layers.
            if os.path.isfile(self.path+'Dockerfile'):
                os.system('/usr/bin/docker build -t builder'+self.aux_id+' '+self.path)
                os.system("/usr/bin/docker save builder"+self.aux_id+" > "+HYCACHE+self.aux_id+"/building/container.tar")
            else:
                l.LOGGER("Error: Dockerfile no encontrado.")
            os.system("tar -xvf "+HYCACHE+self.aux_id+"/building/container.tar -C "+HYCACHE+self.aux_id+"/building/")

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

            filesystem = recursive_parsing( directory = HYCACHE+self.aux_id+"/filesystem/" )

            self.service.container.filesystem.CopyFrom( filesystem )

            return celaut.Any.Metadata.HashTag(
                hash = calculate_hashes( value = filesystem.SerializeToString() )
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
                    except FileNotFoundError: self.service.tensor.index[var] = celaut.FieldDef()
        
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

    def save(self):
        service_buffer = self.service.SerializeToString()
        self.metadata.complete = True
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
        with open( REGISTRY + id, 'wb') as f:
            f.write(
                    celaut.Any(
                        metadata = self.metadata,
                        value = service_buffer
                    ).SerializeToString()
                )
        return id

def ok(path, aux_id):
    Hyperfile = Hyper(path = path, aux_id = aux_id)

    Hyperfile.parseContainer()
    Hyperfile.parseApi()
    Hyperfile.parseLedger()
    Hyperfile.parseTensor()

    return Hyperfile.save()

if __name__ == "__main__":
    import random
    aux_id = str(random.random())
    git = str(sys.argv[1])
    repo = git.split('::')[0]
    branch = git.split('::')[1]
    os.system('git clone --branch '+branch+' '+repo+' '+HYCACHE+aux_id+'/for_build/git')
    l.LOGGER(str(os.listdir(HYCACHE+aux_id+'/for_build/git/.service/')))
    id = ok(
        path = HYCACHE+aux_id+'/for_build/git/.service/',
        aux_id = aux_id
        )  # Hyperfile

    os.system('/usr/bin/docker tag builder'+aux_id+' '+id+'.docker')
    os.system('/usr/bin/docker rmi builder'+aux_id)
    os.system('rm -rf '+HYCACHE+aux_id+'/')
    print(id)

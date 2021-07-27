import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/home/hy/node/app.log"),
        logging.StreamHandler()
    ]
    )
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

import sys
import json
from subprocess import run, check_output
import os
import gateway_pb2
from verify import get_service_list_of_hashes, calculate_hashes

# DIRECTORIES
HYCACHE = "/home/hy/node/__hycache__/"
REGISTRY = "/home/hy/node/__registry__/"

class Hyper:
    def __init__(self, path, aux_id):
        super().__init__()
        self.service =  gateway_pb2.ipss__pb2.Service()
        self.path = path
        self.json = json.load(open(self.path+"service.json", "r"))
        self.aux_id = aux_id

    def parseFilesys(self):
        # Directories are created on cache.
        os.system("mkdir "+HYCACHE+self.aux_id+"/building")
        os.system("mkdir "+HYCACHE+self.aux_id+"/filesystem")

        # Build container and get compressed layers.
        if os.path.isfile(self.path+'Dockerfile'):
            os.system('/usr/bin/docker build -t builder'+self.aux_id+' '+self.path)
            os.system("/usr/bin/docker save builder"+self.aux_id+" > "+HYCACHE+self.aux_id+"/building/container.tar")
        else:
            LOGGER("Error: Dockerfile no encontrado.")
        os.system("tar -xvf "+HYCACHE+self.aux_id+"/building/container.tar -C "+HYCACHE+self.aux_id+"/building/")

        # Save his filesystem on cache.
        for layer in os.listdir(HYCACHE+self.aux_id+"/building/"):
            if os.path.isdir(HYCACHE+self.aux_id+"/building/"+layer):
                LOGGER('Unzipping layer '+layer)
                os.system("tar -xvf "+HYCACHE+self.aux_id+"/building/"+layer+"/layer.tar -C "+HYCACHE+self.aux_id+"/filesystem/")

        # Add filesystem data to filesystem buffer object.
        def recursive_parsing(directory: str) -> gateway_pb2.ipss__pb2.Filesystem:
            filesystem = gateway_pb2.ipss__pb2.Filesystem()
            for b_name in os.listdir(directory):

                if b_name == '.wh..wh..opq': 
                    # https://github.com/opencontainers/image-spec/blob/master/layer.md#opaque-whiteout
                    LOGGER('docker opaque witeout file.')
                    continue
                branch = gateway_pb2.ipss__pb2.Filesystem.Branch()
                branch.name = os.path.basename(b_name)

                # It's a link.
                if os.path.islink(directory+b_name):
                    LOGGER('    Adding link '+ b_name)
                    branch.link = os.path.realpath(directory+b_name)
                    filesystem.branch.append(branch)
                    continue

                # It's a file.
                if os.path.isfile(directory+b_name):
                    LOGGER('    Adding file '+ b_name)
                    with open(directory+b_name, 'rb') as file:
                        branch.file = file.read()
                    filesystem.branch.append(branch)
                    continue

                # It's a folder.
                if os.path.isdir(directory+b_name):
                    LOGGER('    Adding directory '+ b_name)
                    branch.filesystem.CopyFrom(
                        recursive_parsing(directory=directory+b_name+'/')
                        )
                    filesystem.branch.append(branch)
                    continue

            return filesystem

        filesystem = recursive_parsing(directory=HYCACHE+self.aux_id+"/filesystem/")

        self.service.container.filesystem.CopyFrom( filesystem )

        self.service.container.filesystem.hash.extend(
            calculate_hashes( filesystem.SerializeToString() )
        )


    def parseContainer(self):
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
        self.service.container.architecture.hash.append( self.json.get('architecture') )

        # Filesystem
        self.parseFilesys()


    def parseApi(self):
        try:
            with open(self.path + "api.application", "rb") as api_desc:
                self.service.api.app_protocol.ParseFromString(api_desc.read())
        except:
            pass
        if self.json.get('api'):
            # iterate slots.
            for item in self.json.get('api'):
                slot = gateway_pb2.ipss__pb2.Slot()
                # port.
                slot.port = item.get('port')
                # transport protocol.
                slot.transport_protocol.hash.extend(item.get('protocol'))
                self.service.api.slot.append(slot)

    def parseLedger(self):
        if self.json.get('ledger'):
            self.service.ledger.hash = self.json.get('ledger')

    def parseTensor(self):
        tensor = self.json.get('tensor') or None
        if tensor:
            self.service.tensor.rank = tensor["rank"] or None
            index = tensor.get('index') or None
            if index:
                for var in index:
                    variable = gateway_pb2.ipss__pb2.Tensor.Index()
                    variable.id = var
                    for tag in index[var]:
                        variable.tag.append(tag)
                    try:
                        with open(self.path+var+".field", "rb") as var_desc:
                            variable.field.ParseFromString(var_desc.read())
                    except FileNotFoundError: pass
                    self.service.tensor.index.append(variable)

    def save(self):
        self.service.hash.extend(
            get_service_list_of_hashes(self.service)
        )
        # Once service hashes are calculated, we prune the filesystem for save storage.
        self.service.container.filesystem.ClearField('branch')
        for hash in self.service.hash:
            if "sha3-256" == hash[:8]:
                id = hash[9:]
        with open( REGISTRY +id+ '.service', 'wb') as f:
            f.write( self.service.SerializeToString() )
        return id

def ok(path, aux_id):
    Hyperfile = Hyper(path=path, aux_id=aux_id)

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
    LOGGER(os.listdir(HYCACHE+aux_id+'/for_build/git/.service/'))
    id = ok(path=HYCACHE+aux_id+'/for_build/git/.service/', aux_id=aux_id)  # Hyperfile

    os.system('/usr/bin/docker tag builder'+aux_id+' '+id+'.service')
    os.system('/usr/bin/docker rmi builder'+aux_id)
    os.system('rm -rf '+HYCACHE+aux_id+'/')

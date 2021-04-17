import sys
import json
from subprocess import run, check_output
import os
import gateway_pb2
from verify import calculate_hashes

import logging
logging.basicConfig(filename='/home/hy/node/app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

# DIRECTORIES
HYCACHE = "/home/hy/node/__hycache__/"
REGISTRY = "/home/hy/node/__registry__/"

class Hyper:
    def __init__(self, path, aux_id):
        super().__init__()
        self.service =  gateway_pb2.ipss__pb2.Service()
        self.service_with_hashes = gateway_pb2.ipss__pb2.Service()
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
        
        self.service.container.filesystem.CopyFrom(
            recursive_parsing(directory=HYCACHE+self.aux_id+"/filesystem/")
        )
        
        self.service_with_hashes.container.filesystem.hash.extend(
            calculate_hashes( self.service.container.filesystem.SerializeToString() )
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
        
        self.service_with_hashes.MergeFrom(self.service)

        # Arch
        self.service_with_hashes.container.architecture.hash.append( self.json.get('architecture') )

        # Filesystem
        self.parseFilesys() # TODO


    def parseApi(self):
        if self.json.get('api'):
            # iterate slots.
            for item in self.json.get('api'):
                slot = gateway_pb2.ipss__pb2.Slot()
                # port.
                slot.port = item.get('port')
                # aplication protocol.
                with open(self.path+str(slot.port)+".application", "rb") as api_desc:
                    slot.application_protocol.ParseFromString(api_desc.read())
                self.service.api.append(slot)

                # transport protocol.
                slot_with_hash = gateway_pb2.ipss__pb2.Slot()
                slot_with_hash.CopyFrom(slot)
                slot_with_hash.transport_protocol.hash.extend(item.get('protocol'))

                self.service_with_hashes.api.append(slot_with_hash)

    def parseLedger(self):
        if self.json.get('ledger'):
            self.service_with_hashes.ledger.hash = self.json.get('ledger')

    def parseTensor(self):
        tensor = self.json.get('tensor') or None
        if tensor:
            input = tensor.get('input') or None
            if input:
                for var in input:
                    variable = gateway_pb2.ipss__pb2.Tensor.Variable()
                    variable.id = var
                    for tag in input[var]:
                        variable.tag.append(tag)
                    try:
                        with open(self.path+var+".field", "rb") as var_desc:
                            variable.field.ParseFromString(var_desc.read())
                    except FileNotFoundError: pass
                    self.service.tensor.input_variable.append(variable)
            output = tensor.get('output') or None
            if output:
                for var in output:
                    variable = gateway_pb2.ipss__pb2.Tensor.Variable()
                    variable.id = var
                    for tag in output[var]:
                        variable.tag.append(tag)
                    try:
                        with open(self.path+var+".field", "rb") as var_desc:
                            variable.field.ParseFromString(var_desc.read())
                    except FileNotFoundError: pass
                    self.service.tensor.output_variable.append(variable)
            self.service_with_hashes.tensor.CopyFrom(self.service.tensor)

    def save(self):
        self.service_with_hashes.hash.extend(
            calculate_hashes(self.service.SerializeToString())
        )
        for hash in self.service_with_hashes.hash:
            if "sha3-256" == hash[:8]:
                id = hash[9:]
        with open( REGISTRY +id+ '.service', 'wb') as f:
            f.write( self.service_with_hashes.SerializeToString() )
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
    if len(sys.argv) == 1:
        id = ok(path='/home/hy/node/__hycache__/'+aux_id+'/for_build/', aux_id=aux_id)  # Hyperfile
    elif len(sys.argv) == 2:
        git = str(sys.argv[1])
        repo = git.split('::')[0]
        branch = git.split('::')[1]
        os.system('git clone --branch '+branch+' '+repo+' '+HYCACHE+aux_id+'/for_build/git')
        LOGGER(os.listdir(HYCACHE+aux_id+'/for_build/git/.service/'))
        id = ok(path=HYCACHE+aux_id+'/for_build/git/.service/', aux_id=aux_id)  # Hyperfile
    else:
        LOGGER('NO SE ACEPTAN MAS PARÁMETROS..')

    os.system('/usr/bin/docker tag builder'+aux_id+' '+id+'.service')
    os.system('/usr/bin/docker rmi builder'+aux_id)
    os.system('rm -rf '+HYCACHE+aux_id+'/')

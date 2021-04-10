import sys
import json
from subprocess import run, check_output
import os
import hashlib
import gateway_pb2

import logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

# DIRECTORIES
HYCACHE = "/home/hy/node/__hycache__/"
REGISTRY = "/home/hy/node/__registry__/"

# -- HASH FUNCTIONS --
SHAKE_256 = lambda value: "" if value is None else hashlib.shake_256(value).hexdigest(32)
SHA3_256 = lambda value: "" if value is None else hashlib.sha3_256(value).hexdigest()

# ALERT: Its not async.
SHAKE_STREAM = lambda value: "" if value is None else hashlib.shake_256(value).hexdigest(99999999)

class Hyper:
    def __init__(self, path, aux_id):
        super().__init__()
        self.file = gateway_pb2.ServiceFile()
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

        # Give permissions to the filesystem folder.
        os.system("sudo chown -R hy "+HYCACHE+self.aux_id+"/filesystem/")
        
        # Add filesystem data to filesystem buffer object.
        def recursive_parsing(directory: str) -> gateway_pb2.ipss__pb2.Filesystem:
            filesystem = gateway_pb2.ipss__pb2.Filesystem()
            for b_name in os.listdir(directory):
                
                if b_name == '.wh..wh..opq': 
                    # https://github.com/opencontainers/image-spec/blob/master/layer.md#opaque-whiteout
                    LOGGER('docker opaque witeout file.')
                    continue
                branch = gateway_pb2.ipss__pb2.Filesystem.Branch()
                branch.name = b_name

                # It's a file.
                if os.path.isfile(directory+b_name):
                    LOGGER('    Adding file '+ b_name)
                    with open(directory+b_name, 'rb') as file:
                        branch.file = file.read()

                # It's a folder.
                if os.path.isdir(directory+b_name):
                    LOGGER('    Adding directory '+ b_name)
                    branch.filesystem.CopyFrom(
                        recursive_parsing(directory=directory+b_name+'/')
                        )
                    
                filesystem.branch.append(branch)
            return filesystem
        
        self.file.service.container.filesystem.CopyFrom(
            recursive_parsing(directory=HYCACHE+self.aux_id+"/filesystem/")
        )

    def parseContainer(self):
        # Arch
        self.file.service.container.architecture.tag.extend( self.json.get('arquitecture') )
        
        # Envs
        if self.json.get('envs'):
            for env in self.json.get('envs'):
                try:
                    with open(self.path+env+".field", "rb") as env_desc:
                        self.file.service.container.enviroment_variables[env].ParseFromString(env_desc.read())
                except FileNotFoundError:
                    self.file.service.container.enviroment_variables[env]
        
        # Entrypoint
        if self.json.get('entrypoint'):
            self.file.service.container.entrypoint = self.json.get('entrypoint')
        
        # Filesystem
        self.parseFilesys() # TODO

    def parseApi(self):
        if self.json.get('api'):
            # iterate slots.
            for item in self.json.get('api'):
                slot = gateway_pb2.ipss__pb2.Slot()
                # port.
                slot.port = item.get('port')
                # transport protocol.
                slot.transport_protocol.tag.extend(item.get('protocol'))  # Solo toma una lista de tags ...
                # aplication protocol.
                with open(self.path+str(slot.port)+".application", "rb") as api_desc:
                    slot.application_protocol.ParseFromString(api_desc.read())
                self.file.service.api.append(slot)

    def parseLedger(self):
        if self.json.get('ledger'):
            self.file.service.ledger.tag = self.json.get('ledger')

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
                    self.file.service.tensor.input_variable.append(variable)
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
                    self.file.service.tensor.output_variable.append(variable)

    def save(self):
        # Calculate multi-hash.
        list_of_hashes = ["SHAKE_256", "SHA3_256"]
        for hash_name in list_of_hashes:
            hash = gateway_pb2.Hash()
            hash.tag.append(hash_name)
            hash.hash = eval(hash_name)(self.file.service.SerializeToString())
            self.file.hash.append(hash)

        for hash in self.file.hash:
            if "SHA3_256" in hash.tag:
                id = hash.hash
        with open( REGISTRY +id+ '.service', 'wb') as f:
            f.write( self.file.SerializeToString() )
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
        os.system('git clone --branch '+branch+' '+repo+' /home/hy/node/__hycache__/'+aux_id+'/for_build/git')
        LOGGER(os.listdir('/home/hy/node/__hycache__/'+aux_id+'/for_build/git/.service/'))
        id = ok(path='/home/hy/node/__hycache__/'+aux_id+'/for_build/git/.service/', aux_id=aux_id)  # Hyperfile
    else:
        LOGGER('NO SE ACEPTAN MAS PARÁMETROS..')

    os.system('/usr/bin/docker tag builder'+aux_id+' '+id)
    os.system('/usr/bin/docker rmi builder'+aux_id)
    os.system('rm -rf /home/hy/node/__hycache__/'+aux_id+'/*')

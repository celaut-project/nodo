import sys
import json
from subprocess import run, check_output
import os
import hashlib
import gateway_pb2

import logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

# -- HASH FUNCTIONS --
SHAKE_256 = lambda value: "" if value is None else hashlib.shake_256(value.encode()).hexdigest(32)
SHA3_256 = lambda value: "" if value is None else hashlib.sha3_256(value.encode()).hexdigest(32)

# ALERT: Its not async.
SHAKE_STREAM = lambda value: "" if value is None else hashlib.shake_256(value.encode()).hexdigest(99999999)

def calculate_service_hash(service, hash_function: str):
    aux_service = gateway_pb2.ipss__pb2.Service()
    aux_service.CopyFrom(service)
    aux_fs = gateway_pb2.ipss__pb2.Container.Filesystem()
    for hash in aux_service.container.filesystem:
        if hash.algorithm == hash_function:
            aux_fs.append(hash)
    aux_service.container.filesystem.CopyFrom(aux_fs)
    HASH = eval(hash_function)
    return HASH(aux_service.SerializeToString())

class Hyper:
    def __init__(self, path, aux_id):
        super().__init__()
        self.file = gateway_pb2.ServiceFile()
        self.path = path
        self.json = json.load(open(self.path+"service.json", "r"))
        self.aux_id = aux_id

    def parseFilesys(self):
        os.system("mkdir /home/hy/node/__hycache__/"+self.aux_id+"/building")
        if os.path.isfile(self.path+'Dockerfile'):
            os.system('/usr/bin/docker build -t builder'+self.aux_id+' '+self.path)
            os.system("/usr/bin/docker save builder"+self.aux_id+" | gzip > /home/hy/node/__hycache__/"+self.aux_id+"/building/building.tar.gz")
        else:
            ("Error: Dockerfile no encontrado.")
        os.system("cd /home/hy/node/__hycache__/"+self.aux_id+"/building && tar -xvf building.tar.gz")
        dirs = [] # lista de todos los directorios para ordenar.
        layers = []
        for layer in os.listdir("/home/hy/node/__hycache__/"+self.aux_id+"/building/"):
            if os.path.isdir("/home/hy/node/__hycache__/"+self.aux_id+"/building/"+layer):
                layers.append(layer)
                LOGGER("Layer --> "+str(layer)) # Si accedemos directamente, en vez de descomprimir, será bastante mas rapido.
                for dir in check_output("cd /home/hy/node/__hycache__/"+self.aux_id+"/building/"+layer+" && tar -xvf layer.tar", shell=True).decode('utf-8').split("\n")[:-1]:
                    if dir.split(' ')[0]=='/' or len(dir)==1:
                        LOGGER("Ghost directory --> "+dir)
                        continue # Estos no se de donde salen.
                    dirs.append(dir)
        def create_tree(index, dirs, layers):
            def add_file(adir, layers):
                for layer in layers:
                    if os.path.exists('/home/hy/node/__hycache__/'+self.aux_id+'/building/'+layer+'/'+adir):
                        if os.path.isfile('/home/hy/node/__hycache__/'+self.aux_id+'/building/'+layer+'/'+adir):
                            if adir == '.wh..wh..opq':
                                return None
                            LOGGER("Archivo --> "+adir)
                            cdir = '/home/hy/node/__hycache__/'+self.aux_id+'/building/'+layer+'/'+adir
                            try:
                                info = open(cdir,"r").read()
                            except UnicodeDecodeError: 
                                info = open(cdir,"br").read().decode('cp437')
                            except FileNotFoundError:
                                LOGGER(adir+" posiblemente vacio.")
                                exit()
                            except UnboundLocalError:
                                LOGGER("UnboundError")
                                LOGGER(cdir)
                                exit()
                            return {
                                "Dir":adir,
                                "Id":SHA3_256(info)
                            }
                        elif os.path.islink('/home/hy/node/__hycache__/'+self.aux_id+'/building/'+layer+'/'+adir):
                            link = check_output('ls -l /home/hy/node/__hycache__/'+self.aux_id+'/building/'+layer+'/'+adir, shell=True).decode('utf-8').split(" ")[-1]
                            LOGGER("Link --> "+adir)
                            return {
                                "Dir":adir,
                                "Id":SHA3_256(link)
                            }
                        else:
                            LOGGER("ERROR: No deberiamos haber llegado aqui.")
                            os.system("rm -rf /home/hy/node/__hycache__/"+self.aux_id+"/building")
                            os.system("/usr/bin/docker rmi /home/hy/node/__hycache__/"+self.aux_id+"/building")
                            exit()
                        """elif os.path.ismount('__hycache__/building/'+layer+'/'+adir):
                            LOOGGER("Mount --> "+adir)
                            LOGGER("ERROR: No deberiamos haber llegado aqui.")
                            os.system("rm -rf __hycache__/building")
                            os.system("/usr/bin/docker rmi __hycache__/building")
                            exit()
                        elif os.path.isabs('__hycache__/building/'+layer+'/'+adir):
                            LOGGER("Abs -->"+adir)
                            LOGGER("ERROR: No deberiamos haber llegado aqui.")
                            os.system("rm -rf __hycache__/building")
                            os.system("/usr/bin/docker rmi __hycache__/building")
                            exit()"""
                LOGGER("Algo fue mal. No se encontro en ninguna capa ¿?")
            LOGGER("           Nueva vuelta"+str(index))
            local_dirs={}
            local_files={}
            for dir in dirs:
                LOGGER("Directory --> "+dir)
                raiz = dir.split('/')[index]
                LOGGER("Raiz --> "+raiz)
                if dir[-1]!="/" and dir.split('/')[-1]==raiz:
                    d = add_file(adir=dir, layers=layers)
                    if d is not None:
                        local_files.update({raiz:d})
                else:
                    if (raiz in local_dirs) == False:
                        LOGGER('   Nueva raiz --> '+raiz)
                        local_dirs.update({raiz:[]})
                    if dir[-1]!="/" or raiz!=dir.split('/')[-2]: # No introducimos usr/ si la raiz es usr.
                        lista = local_dirs[raiz]
                        lista.append(dir)
                        local_dirs.update({raiz:lista})
            for raiz in local_dirs:
                local_dirs.update({raiz:create_tree(index=index+1,dirs=local_dirs[raiz], layers=layers)})
            return {**local_dirs, **local_files}
        fs_tree = create_tree(index=0,dirs=dirs, layers=layers)
        def make_hash(merkle):
            s=None
            for i in merkle:
                if s == None: s = i.get('Id')
                s = s+' '+i.get('Id')
            return SHA3_256(s)
        def reorder_tree(tree):
            l = []
            for d in sorted(tree.items(), key=lambda x: x[0]):
                v = d[1]
                try:
                    if ('Dir' in v and 'Id' in v)==False: # No es un directorio ...
                        merkle = reorder_tree(tree=v)
                        if merkle == []: continue
                        id = make_hash(merkle=merkle)
                        l.append({
                            'Id' : id,
                            'Merkle': merkle
                            })
                    else:
                        l.append(v)
                except AttributeError:
                    LOGGER(v)
                    exit()
            return l
        fs_tree = reorder_tree(fs_tree)
        fs_tree = {
            "Id": make_hash(fs_tree),
            "Merkle": fs_tree
        }
        hash = gateway_pb2.ipss__pb2.Hash()
        hash.algorithm = "SHA3_256"
        hash.hash = fs_tree["Id"]
        self.file.service.container.filesystem.append(hash)

    def parseContainer(self):
        # Arch
        self.file.service.container.architecture.tag.extend( self.json.get('arquitecture') )
        # Envs
        if self.json.get('envs'):
            self.file.service.container.enviroment_variables.extend( self.json.get('envs') )
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
                if os.path.isfile(self.path+" "+str(slot.port)+".api"): # los proto file son del tipo 8080.api
                    with open(self.path+" "+str(slot.port)+".api") as api_desc:
                        slot.aplication_protocol.ParseFromString(api_desc.read())
                self.file.service.api.slot.append(slot)

    def parseLedger(self):
        if self.json.get('ledger'):
            self.file.service.ledger.tag = self.json.get('ledger')

    def parseTensor(self):
        tensor = self.json.get('tensor')
        if tensor:    
            for var in tensor.get('output_variables'):
                variable = gateway_pb2.ipss__pb2.Tensor.Variable()
                variable.tag.extend(var.get("tags"))
                # Aun no se especifica el cuerpo field para este compilador.
                self.file.service.tensor.output_variable.append(variable)
            for var in tensor.get('input_variables'):
                variable.tag.extend(var.get("tags"))
                # Aun no se especifica el cuerpo field para este compilador.
                self.file.service.tensor.input_variable.append(variable)


    def save(self):
        # Calculate multi-hash.
        list_of_hashes = ["SHAKE_256", "SHA3_256"]
        for hash_name in list_of_hashes:
            hash = gateway_pb2.ipss__pb2.Hash()
            hash.algorithm = hash_name
            hash.hash = calculate_service_hash(service=self.file.service, hash_function=hash_name)
            self.file.multihash.append(hash)

        for hash in self.file.multihash:
            if hash.algorithm == "SHA3_256":
                id = hash.algorithm
        with open( '/home/hy/node/__registry__/' +id+ '.service', 'wb') as f:
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

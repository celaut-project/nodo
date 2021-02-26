import sys
import json
from subprocess import run, check_output
import os
import hashlib
import ipss_pb2

import logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

SHAKE_256 = lambda value: "" if value is None else hashlib.shake_256(value.encode()).hexdigest(32)
SHA3_256 = lambda value: "" if value is None else hashlib.sha3_256(value.encode()).hexdigest(32)

# ALERT: Its not async.
SHAKE_STREAM = lambda value: "" if value is None else hashlib.shake_256(value.encode()).hexdigest(99999999)

class Hyper:
    def __init__(self, path):
        super().__init__()
        self.file = ipss_pb2.ExtendedService()
        self.path = path

    def parseDependency(self):
        dependencies = []
        if os.path.isdir(self.path+'dependencies'):
            for file in os.listdir(self.path+'dependencies'):
                image = json.load(open(self.path+'dependencies/'+file,'r'))
                dependencies.append(image)
            if len(dependencies)>0:
                self.file.service.update({'Dependency':dependencies})

    def parseFilesys(self):
        os.system("mkdir /home/hy/node/__hycache__/building")
        if os.path.isfile(self.path+'Dockerfile'):
            os.system('/usr/bin/docker build -t building '+self.path)
            os.system("/usr/bin/docker save building | gzip > /home/hy/node/__hycache__/building/building.tar.gz")
        elif os.path.isfile(self.path+'building.tar.gz'):
            os.system("mv "+self.path+"building.tar.gz __hycache__/building/")
        else:
            ("Error: Dockerfile o building.tar.gz no encontrados.")
        os.system("cd /home/hy/node/__hycache__/building && tar -xvf building.tar.gz")
        dirs = [] # lista de todos los directorios para ordenar.
        layers = []
        for layer in os.listdir("/home/hy/node/__hycache__/building/"):
            if os.path.isdir("/home/hy/node/__hycache__/building/"+layer):
                layers.append(layer)
                LOGGER("Layer --> "+str(layer)) # Si accedemos directamente, en vez de descomprimir, será bastante mas rapido.
                for dir in check_output("cd /home/hy/node/__hycache__/building/"+layer+" && tar -xvf layer.tar", shell=True).decode('utf-8').split("\n")[:-1]:
                    if dir.split(' ')[0]=='/' or len(dir)==1:
                        LOGGER("Ghost directory --> "+dir)
                        continue # Estos no se de donde salen.
                    dirs.append(dir)
        def create_tree(index, dirs, layers):
            def add_file(adir, layers):
                for layer in layers:
                    if os.path.exists('/home/hy/node/__hycache__/building/'+layer+'/'+adir):
                        if os.path.isfile('/home/hy/node/__hycache__/building/'+layer+'/'+adir):
                            if adir == '.wh..wh..opq':
                                return None
                            LOGGER("Archivo --> "+adir)
                            cdir = '/home/hy/node/__hycache__/building/'+layer+'/'+adir
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
                                "Id":SHAKE(info)
                            }
                        elif os.path.islink('/home/hy/node/__hycache__/building/'+layer+'/'+adir):
                            link = check_output('ls -l /home/hy/node/__hycache__/building/'+layer+'/'+adir, shell=True).decode('utf-8').split(" ")[-1]
                            LOGGER("Link --> "+adir)
                            return {
                                "Dir":adir,
                                "Id":SHAKE(link)
                            }
                        else:
                            LOGGER("ERROR: No deberiamos haber llegado aqui.")
                            os.system("rm -rf /home/hy/node/__hycache__/building")
                            os.system("/usr/bin/docker rmi /home/hy/node/__hycache__/building")
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
            return SHAKE(s)
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

    def parseContainer(self):
        # Arch
        self.file.service.container.architecture.tag.extend( json.load(open(self.path+"Arch.json", "r")) )
        # Envs
        if os.path.isfile(self.path+"Envs.json"):
            self.file.service.container.enviroment_variables.extend( json.load(open(self.path+"Envs.json", "r")) )
        # Entrypoint
        if os.path.isfile(self.path+"Entrypoint.json"):
            self.file.service.container.entrypoint = json.load(open(self.path+"Entrypoint.json", "r"))
        # Filesystem
        self.parseFilesys() # TODO

    def parseApi(self):
        if os.path.isfile(self.path+"Api.json"):
            for item in json.load(open(self.path+"Api.json", "r")):
                slot = ipss_pb2.Slot()
                slot.port = item.get('port')
                slot.transport_protocol.tag.extend(item.get('protocol'))  # Solo toma una lista de tags ...
                if os.path.isfile(self.path+" "+slot.port+".desc"): # los proto file son del tipo 8080.proto
                    with open(self.path+" "+slot.port+".desc") as api_desc:
                        slot.aplication_protocol.ParseFromString(api_desc.read())
                self.file.service.api.slot.append(slot)

    def parseLedger(self):
        if os.path.isfile(self.path+"Ledger.json"):
            self.file.service.ledger.tag = json.load(open(self.path+"Ledger.json", "r"))

    def parseTensor(self):
        if os.path.isfile(self.path+"Tensor.json"):
            tensor = json.load(open(self.path+"Tensor.json", "r"))
            for var in tensor.get('output_variables'):
                variable = ipss_pb2.Tensor.Variable()
                variable.tag.extend(var.get("tags"))
                # Aun no se especifica el cuerpo field para este compilador.
                self.file.service.tensor.output_variable.append(variable)
            for var in tensor.get('input_variables'):
                variable.tag.extend(var.get("tags"))
                # Aun no se especifica el cuerpo field para este compilador.
                self.file.service.tensor.input_variable.append(variable)

    @staticmethod
    def calculateMultihash(data):
        multihash = ipss_pb2.Multihash()
        multihash.hash["SHA3_256"] = SHA3_256(data)
        multihash.hash["SHAKE_256"] = SHAKE_256(data)
        return multihash

    def save(self):
        self.file.multihash.CopyFrom(
            Hyper.calculateMultihash(
                data = self.file.service.SerializeFromString()
            )
        )
        id = SHA3_256( self.file.service.SerializeFromString() )
        file_dir = '/home/hy/node/__registry__/' +id+ '.bin'
        with open(file_dir,'wb') as f:
            f.write( self.file.SerializeToString() )
        os.system('mkdir /home/hy/node/__registry__/'+ id)
        os.system('mv '+self.path+'* /home/hy/node/__registry__/'+ id +'/')

def ok(path):
    Hyperfile = Hyper(path=path)

    Hyperfile.parseContainer()
    Hyperfile.parseApi()
    Hyperfile.parseDependency()
    Hyperfile.parseLedger()
    Hyperfile.parseTensor()

    Hyperfile.save()

if __name__ == "__main__":
    if len(sys.argv) == 1:
        ok(path='/home/hy/node/__hycache__/for_build/')  # Hyperfile
    elif len(sys.argv) == 2:
        git = str(sys.argv[1])
        repo = git.split('::')[0]
        branch = git.split('::')[1]
        os.system('git clone --branch '+branch+' '+repo+' /home/hy/node/__hycache__/for_build/git')
        LOGGER(os.listdir('/home/hy/node/__hycache__/for_build/git/.hy/'))
        ok(path='/home/hy/node/__hycache__/for_build/git/.hy/')  # Hyperfile
    else:
        LOGGER('NO SE ACPTAN MAS PARÁMETROS..')

    os.system('/usr/bin/docker rmi building')
    os.system('rm -rf /home/hy/node/__hycache__/*')

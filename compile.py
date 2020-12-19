import sys
import json
from subprocess import run, check_output
import os
import hashlib

import logging
logging.basicConfig(filename='app.log', filemode='w', format='%(name)s - %(levelname)s - %(message)s')
LOGGER = lambda message: logging.getLogger(__name__).debug(message)

SHAKE = lambda value: "" if value is None else hashlib.shake_256(value.encode()).hexdigest(32)

# ALERT: Its not async.
SHAKE_STREAM = lambda value: "" if value is None else hashlib.shake_256(value.encode()).hexdigest(99999999)

class Hyper:
    def __init__(self, path, file={
                "Api": None,        # list
                "Container" : None, # dict
                "Contract": None,   # list
                "Dependency": None, # list
                "Ledger": None, 
                "Tensor": None,
            }):
        super().__init__()
        self.file = file
        self.path = path

    def parseDependency(self):
        dependencies = []
        if os.path.isdir(self.path+'dependencies'):
            for file in os.listdir(self.path+'dependencies'):
                image = json.load(open(self.path+'dependencies/'+file,'r'))
                dependencies.append(image)
            if len(dependencies)>0:
                self.file.update({'Dependency':dependencies})

    def parseContainer(self):
        def parseFilesys(container):
            os.system("mkdir /home/hy/node/__hycache__/building")
            if os.path.isfile(self.path+'Dockerfile'):
                os.system('docker build -t building '+self.path)
                os.system("docker save building | gzip > /home/hy/node/__hycache__/building/building.tar.gz")
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
                                os.system("docker rmi /home/hy/node/__hycache__/building")
                                exit()
                            """elif os.path.ismount('__hycache__/building/'+layer+'/'+adir):
                                LOOGGER("Mount --> "+adir)
                                LOGGER("ERROR: No deberiamos haber llegado aqui.")
                                os.system("rm -rf __hycache__/building")
                                os.system("docker rmi __hycache__/building")
                                exit()
                            elif os.path.isabs('__hycache__/building/'+layer+'/'+adir):
                                LOGGER("Abs -->"+adir)
                                LOGGER("ERROR: No deberiamos haber llegado aqui.")
                                os.system("rm -rf __hycache__/building")
                                os.system("docker rmi __hycache__/building")
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
            container.update({'Filesys':fs_tree})
            return container
        container = self.file.get('Container')
        if container == None:
            container = {
                "Entrypoint" : None,    # string
                "Filesys" : None,        # Merkle
                "Arch" : None,          # list
                "Envs": None          # list
            }
        arch = json.load(open(self.path+"Arch.json", "r"))
        container.update({'Arch' : arch})
        if os.path.isfile(self.path+"Envs.json"):
            envs = json.load(open(self.path+"Envs.json", "r"))
            container.update({'Envs' : envs})
        if os.path.isfile(self.path+"Entrypoint.json"):
            entrypoint = json.load(open(self.path+"Entrypoint.json", "r"))
            container.update({'Entrypoint' : entrypoint})
        container = parseFilesys(container=container)
        self.file.update({'Container' : container})

    def parseApi(self):
        if os.path.isfile(self.path+"Api.json"):
            api = json.load(open(self.path+"Api.json", "r"))
            self.file.update({'Api' : api})


    def parseLedger(self):
        pass

    def parseTensor(self):
        pass
    
    @staticmethod
    def getId(hyperfile):
        info = json.dumps(hyperfile)
        LOGGER(info)
        return SHAKE(info)

    def save(self):
        id = Hyper.getId(hyperfile=self.file) 
        file_dir = '/home/hy/node/__registry__/' +id+ '.json'
        with open(file_dir,'w') as f:
            f.write( json.dumps(self.file) )
        os.system('mkdir /home/hy/node/__registry__/'+id)
        os.system('mv '+self.path+' home/hy/__registry__/'+id+'/')

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

    if os.path.isfile('__hycache__/for_build/Arch.json') == False:
        LOGGER('ForBuild invalido, Arch.json OBLIGATORIOS ....')
        exit()

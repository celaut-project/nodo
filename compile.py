import sys
import json
from subprocess import run, check_output
import os
import hashlib


def sha256(val):
    if val is None: return ""
    return "sha256:"+hashlib.sha256(val.encode()).hexdigest()

class Hyper:
    def __init__(self, file={
                "Api": None,        # list
                "Container" : None, # dict
                "Contract": None,   # list
                "Dependency": None, # list
                "Ledger": None, 
                "Tensor": None,
            }):
        super().__init__()
        self.file = file
        self.registry = 'registry/'

    def parseDependency(self):
        dependencies = []
        if os.path.isdir('registry/for_build/dependencies'):
            for file in os.listdir('registry/for_build/dependencies'):
                image = json.load(open(file,'r'))
                dependencies.append(image)
            if len(dependencies)>0:
                self.file.update({'Dependency':dependencies})

    def parseContainer(self):
        def parseFilesys(container):
            os.system("mkdir building")
            os.system('sudo docker build -t building registry/for_build/.')
            os.system("docker save building | gzip > building/building.tar.gz")
            os.system("cd building && tar -xvf building.tar.gz")
            dirs = [] # lista de todos los directorios para ordenar.
            layers = []
            for layer in os.listdir("building/"):
                if os.path.isdir("building/"+layer):
                    layers.append(layer)
                    print("Layer --> ",layer) # Si accedemos directamente, en vez de descomprimir, será bastante mas rapido.
                    for dir in check_output("cd building/"+layer+" && tar -xvf layer.tar", shell=True).decode('utf-8').split("\n")[:-1]:
                        dirs.append(dir)
            def file_hash(adir, layers):
                for layer in layers:
                    if os.path.isfile('building/'+layer+'/'+adir):
                        cdir = 'building/'+layer+'/'+adir
                try:
                    info = open(cdir,"r").read()
                except UnicodeDecodeError:
                    info = open(cdir,"br").read().decode('cp437')
                except FileNotFoundError:
                    print(adir+" posiblemente vacio.")
                return info
            def rec_hash(index, dirs, layers):
                local_dirs={}
                for dir in dirs:
                    raiz = dir.split('/')[index]
                    if raiz in local_dirs==False:
                        local_dirs.update({raiz:[]})
                    if dir!=raiz:
                        lista = local_dirs.get(raiz).append(dir)
                        local_dirs.update({raiz:lista})
                for dir in local_dirs:
                    if local_dirs[dir]==[]:
                        local_dirs.update({dir:file_hash(adir=dir, layers=layers)})
                    else:
                        local_dirs.update({dir:rec_hash(index=index+1,dirs=local_dirs[dir], layers=layers)})
                return local_dirs
            merkle = rec_hash(index=0,dirs=dirs, layers=layers)
            print(merkle)
            os.system("rm -rf building")
            os.system("docker rmi building")
            print(merkle)
            container.update({'Filesys':merkle})
            return container
        container = self.file.get('Container')
        if container == None:
            container = {
                "Entrypoint" : None,    # string
                "Filesys" : None,        # Merkle
                "Arch" : None,          # list
                "Envs": None          # list
            }
        arch = json.load(open("registry/for_build/Arch.json","r"))
        container.update({'Arch' : arch})
        if os.path.isfile("registry/for_build/Envs.json"):
            envs = json.load(open("registry/for_build/Envs.json","r"))
            container.update({'Envs' : envs})
        if os.path.isfile("registry/for_build/Entrypoint.json"):
            entrypoint = json.load(open("registry/for_build/Entrypoint.json","r"))
            container.update({'Entrypoint' : entrypoint})
        container = parseFilesys(container=container)
        self.file.update({'Container' : container})

    def parseApi(self):
        if os.path.isfile("registry/for_build/Api.json"):
            api = json.load(open("registry/for_build/Api.json","r"))
            self.file.update({'Api' : api})


    def parseLedger(self):
        pass

    def parseTensor(self):
        pass
    
    @staticmethod
    def getId(hyper):
        return "hash256"

    def save(self):
        registry = self.registry + Hyper.getId(self.file) + '.json'
        with open(registry,'w') as file:
            file.write( json.dumps(self.file, indent=4, sort_keys=True) )

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print("\nIMPORTANTE: el Dockerfile no soporta comandos de varias lineas.")
        Hyperfile = Hyper() # Hyperfile
    else:
        print("\n NO HAY QUE USAR PARAMETROS.")

    if os.path.isfile('registry/for_build/Arch.json') == False:
        print('ForBuild invalido, Arch.json OBLIGATORIOS ....')
        exit()

    Hyperfile.parseContainer()
    Hyperfile.parseApi()
    Hyperfile.parseDependency()
    Hyperfile.parseLedger()
    Hyperfile.parseTensor()

    Hyperfile.save()
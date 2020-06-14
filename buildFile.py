import sys
import json
from subprocess import run
import os
import hashlib

def value(id):
    if id is "": return ""
    else: return id.split(':')[1]

def sha256(val):
    if val is None: return ""
    return hashlib.sha256(val.encode()).hexdigest()
class Hyper:
    def __init__(self, file={
                "Api": None,        # list
                "Container" : None, # dict
                "Contract": None,   # list
                "Merkle": None,     # dict
                "Import": None,     # list
                "Ledger": None, 
                "Tensor": None
            }):
        super().__init__()
        self.file = file
        self.registry = 'registry/'

    def parseContainer(self):
        def parseInspect(container):
            os.system('powershell.exe docker inspect building > inspect.json')
            inspect = json.load(open('inspect.json','r'))[0]
            container.update({'Volumes':inspect.get('Config').get('Volumes')})
            container.update({'WorkingDir':inspect.get('Config').get('WorkingDir')})
            container.update({'Entrypoint' : inspect.get('Config').get('Entrypoint')[2]})
            if container.get('Layers') == None:
                layers = []
                chainId = None
                for layer in inspect.get('RootFS').get('Layers'):
                    if chainId is None: chainId = layer
                    else: chainId = "sha256:"+sha256(value(chainId)+" "+value(layer))
                    layers.append({
                        "DiffId" : layer,
                        "ChainId" : chainId,
                        "Build" : None
                    })
                container.update({'Layers' : layers})
            else:
                pass # Estaria bien comprobar las hashes.
            os.remove("inspect.json")
            return container
        def parseDockerfile(container):
            Dockerfile = open("registry/for_build/Dockerfile", "r")
            layers_in_file = []
            for l in Dockerfile.readlines():
                command = l.split()[0]
                if command == 'RUN' or command == 'FROM':
                    layers_in_file.append(' '.join(l.split()))
            layers = container.get('Layers')[::-1]
            for i, l in enumerate(layers_in_file[::-1]):
                build = layers[i].get('Build')
                if build == None: build = []
                build.append(l)
                layers[i].update({'Build' : build})
            container.update({'Layers' : layers[::-1]})
            Dockerfile.close()
            return container
        container = self.file.get('Container')
        if container == None:
            container = {
                "Volumes" : None,       # list
                "WorkingDir" : None,    # array
                "Entrypoint" : None,    # array
                "Layers" : None,        # list
                "OsArch" : None         # list
            }          
        container = parseInspect(container)
        container = parseDockerfile(container)
        self.file.update({'Container' : container})

    def parseApi(self):
        api = json.load(open("registry/for_build/Api.json","r"))
        self.file.update({'Api' : api})

    def makeMerkle(self):
        def concat(merkle_list):
            id = value(merkle_list[0].get('Id'))
            for merkle in merkle_list[1:]:
                id = id+" "+value(merkle.get('Id'))
            return  id
        def suma(merkle_list):
            id = 0
            for merkle in merkle_list:
                id = id + int(value(merkle.get('Id')),16)
            return hex(id)[2:-(len(hex(id))-64)] # Recorta el resultado de la suma a 64.

        def makeElem(elem):
            id = 'sha256:'+sha256(elem)
            return {
                "Id" : id,
                "Func": "sha256(atr)"
            }

        def makeApi():
            id = 'sha256:'
            return {
                "Id" : id,
                "Func": None,
            }
        def makeContainer():
            def makeEntrypoint():
                return makeElem(self.file.get('Container').get('Entrypoint'))
            def makeLayers():
                def makeLayer(i):
                    def makeBuEl(i):
                        def makeBuild(i):
                            build = self.file.get('Container').get('Layers')[i].get('Build')
                            if build is None:
                                id = 'sha256:'
                                return {
                                    "Id":id,
                                    "Func": "esto es un build."
                                }
                            else:
                                merkle = []
                                for b in build:
                                    merkle.append(makeElem(b))
                                id = 'sha256:'+sha256(suma(merkle))
                                return {
                                    "Id":id,
                                    "Func": "esto es un build.",
                                    "Merkle": merkle
                                }
                        merkle = [
                            makeBuild(i),
                            makeElem(self.file.get('Container').get('Layers')[i].get('ChainId'))
                        ]
                        id = 'sha256:'+sha256(concat(merkle))
                        return {
                            "Id" : id,
                            "Func": "Hacemos una cadena",
                            "Merkle": merkle
                        }
                    if i==0: merkle = [ makeBuEl(i) ]
                    else: merkle = [
                        makeLayer(i-1),
                        makeBuEl(i)
                    ]
                    id = 'sha256:'+sha256(concat(merkle))
                    return {
                        "Id":id,
                        "Func": "Hacemos una cadena",
                        "Merkle": merkle
                    }
                return makeLayer(len(self.file.get('Container').get('Layers'))-1)
            def makeOsArch():
                id = 'sha256:'+sha256(self.file.get('Container').get('OsArch'))
                return {
                    "Id" : id,
                    "Func": None
                }
            def makeVolumes():
                id = 'sha256:'+sha256(self.file.get('Container').get('Volumes'))
                return {
                    "Id" : id,
                    "Func": None
                }
            def makeWorkingDir():
                id = 'sha256:'+sha256(self.file.get('Container').get('WorkingDir'))
                return {
                    "Id" : id,
                    "Func": None
                }
            merkle = [
                makeEntrypoint(),
                makeLayers(),
                makeOsArch(),
                makeVolumes(),
                makeWorkingDir()
            ]
            id = 'sha256:'+sha256(concat(merkle))
            return {
                "Id" : id,
                "Func" : None,
                "Merkle": merkle
            }
        def makeContract():
            id = 'sha256:'
            return {
                "Id" : id,
                "Func": None,
            }
        merkle = [
            makeApi(),
            makeContainer(),
            makeContract()
        ]
        id = sha256(concat(merkle))
        func = "hash de la concatenacion en orden alfabetico de todos los atributos"
        self.file.update({'Merkle' : {'Id':'sha256:'+id, "Func": func, "Merkle":merkle}})

    def save(self):
        registry = self.registry + self.file.get('Merkle').get('Id').split(':')[1] + '.json'
        with open(registry,'w') as file:
            file.write( json.dumps(self.file, indent=4, sort_keys=True) )

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print("\nIMPORTANTE: el Dockerfile no soporta comandos de varias lineas.")
        print("Dockerfile            --> Dejalo fuera.")
        print("./buildFile.py Dockerfile Hyperfile  --> to update the Hyperfile. \n")
        Hyperfile = Hyper() # Hyperfile
    elif len(sys.argv) > 1:
        Hyperfile = Hyper( json.load(open(sys.argv[1],"r")) ) # Hyperfile
            
    run('docker build -t building registry/for_build/.')
    Hyperfile.parseContainer()
    Hyperfile.parseApi()

    Hyperfile.makeMerkle()
    Hyperfile.save()
    run('docker rmi building --force')
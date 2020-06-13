import sys
import json
from subprocess import run
import os
import hashlib

def value(id):
    if id is "": return ""
    else: return id.split(':')[1]

def sha256(id):
    return hashlib.sha256(id.encode()).hexdigest()
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
            Dockerfile = open("Dockerfile", "r")
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
        api = json.load(open("Api.json","r"))
        self.file.update({'Api' : api})

    def makeId(self):
        def concat(merkle_list):
            id = value(merkle_list[0].get('Id'))
            for merkle in merkle_list[1:]:
                id = id+" "+value(merkle.get('Id'))
            return  id
        def suma(merkle_list):
            for merkle in merkle_list:
                id = id + int(value(merkle_list.get('Id')))
            return hex(id)[:-(len(hex(id))-64)] # Recorta el resultado de la suma a 64.

        def makeApi():
            return {
                "Id" : "",
                "Func": None,
            }
        def makeContainer():
            return {
                "Id" : "sha256:2a19bd70fcd4ce7fd73b37b1b2c710f8065817a9db821ff839fe0b4b4560e643",
                "Func" : None,
            }
        def makeContract():
            return {
                "Id" : "",
                "Func": None,
            }
        merkle = [
            makeApi(),
            makeContainer(),
            makeContract()
        ]
        id = concat(merkle)
        func = "concatenacion en orden alfabetico de todos los atributos"
        self.file.update({'Merkle' : {'Id':'sha256:'+id, "Func": func, "Merkle":merkle}})

    def save(self):
        #registry = self.registry + self.file.get('Merkle').get('Id').split(':')[1] + '.json'
        registry = self.registry + 'xx34hjnmkoi9u8y7ghbjnki8u7y6trfghbjkiu8y7tfgv' + '.json'
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
            
    run('docker build -t building .')
    Hyperfile.parseContainer()
    Hyperfile.parseApi()

    Hyperfile.makeId()
    Hyperfile.save()
    #run('docker rmi building --force')
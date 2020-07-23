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
                "Dependency": None, # list
                "Ledger": None, 
                "Tensor": None,
            }):
        super().__init__()
        self.file = file
        self.registry = 'registry/'

    def parseDependency(self):
        dependencies = []
        for file in os.listdir('registry/for_build'):
            if file != 'Dockerfile' and file != 'Api.json':
                image = json.load(open(file,'r'))
                dependencies.append(image)
        if len(dependencies)>0:
            self.file.update({'Dependency':dependencies})

    def parseContainer(self):
        def parseInspect(container):
            run('sudo docker inspect building > inspect.json', shell=True)
            inspect = json.load(open('inspect.json','r'))[0]
            container.update({'Volumes':inspect.get('Config').get('Volumes')})
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
        def parseArchEnvs(container):
            arch = json.load(open("registry/for_build/Arch.json","r"))
            container.update({'Arch' : arch})
            if os.path.isfile("registry/for_build/Envs.json"):
                envs = json.load(open("registry/for_build/Envs.json","r"))
                container.update({'Envs' : envs})
            return container

        container = self.file.get('Container')
        if container == None:
            container = {
                "Volumes" : None,       # list
                "Entrypoint" : None,    # string
                "Layers" : None,        # list
                "Arch" : None,          # list
                "Envs": None          # list or dict
            }          
        container = parseInspect(container)
        container = parseDockerfile(container)
        container = parseArchEnvs(container)
        self.file.update({'Container' : container})

    def parseApi(self):
        if os.path.isfile("registry/for_build/Api.json"):
            api = json.load(open("registry/for_build/Api.json","r"))
            self.file.update({'Api' : api})


    # Esta es la forma en la que este compilador crea el árbol de Merkle.
    # No tiene por que ser la única ...

    def makeMerkle(self):
        def concat(merkle_list):
            id = merkle_list[0].get('Id')
            for merkle in merkle_list[1:]:
                id = id+" "+merkle.get('Id')
            return sha256(id)
        def makeContainer():
            def makeEntrypoint():
                return {
                    "Id":concat(self.file.get('Container').get('Entrypoint')),
                    "$ref":"#/Container/Entrypoint"
                }
            def makeLayers():
                def makeLayer(i):
                    def makeBuEl(i):
                        def makeBuild(i):
                            build = self.file.get('Container').get('Layers')[i].get('Build')
                            if build is None:
                                return {
                                    "Id":""
                                }
                            else:
                                merkle = []
                                for index,b in enumerate(build):
                                    merkle.append({
                                        "Id":concat(b),
                                        "$ref":"#/Container/Layers["+str(i)+"]/Build["+str(index)+"]"
                                    })
                                return {
                                    "Id":concat(merkle)
                                    "Merkle": merkle
                                }
                        merkle = [
                            makeBuild(i),
                            {
                                "id":sha256(self.file.get('Container').get('Layers')[i].get('ChainId')),
                                "$ref":"#/Container/Layers["+str(i)+"]/ChainId"
                            }
                        ]
                        return {
                            "Id" : concat(merkle),
                            "Merkle": merkle
                        }
                    if i==0: merkle = [ makeBuEl(i) ]
                    else: merkle = [
                        makeLayer(i-1),
                        makeBuEl(i)
                    ]
                    return {
                        "Id":concat(merkle),
                        "Merkle": merkle
                    }
                return makeLayer(len(self.file.get('Container').get('Layers'))-1)
            def makeEnvs():
                envs = []
                if self.file.get('Container').get('Envs') == None:
                    return None
                for index, env in enumerate(self.file.get('Container').get('Envs')):
                    envs.append({
                        'Id':sha256(env),
                        '$ref':"#/Container/Envs["+str(index)+"]"
                    })
                return {
                    "Id" : concat(envs)
                    "Merkle": envs
                }
            merkle = [
                makeEntrypoint(),
                makeLayers(),
                makeEnvs()
            ]
            merkle = [make for make in merkle if make != None] # No se concatenan los campos vacios.
            return {
                "Id" : concat(merkle)
                "Merkle": merkle
            }
        merkle = [
            makeContainer()
        ]
        self.file.update({'Merkle' : {'Id':concat(merkle), "Merkle":merkle}})

    def save(self):
        registry = self.registry + value(self.file.get('Merkle').get('Id')) + '.json'
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
            
    run('sudo docker build -t building registry/for_build/.', shell=True)
    Hyperfile.parseContainer()
    Hyperfile.parseApi()

    Hyperfile.makeMerkle()
    Hyperfile.save()
    run('sudo docker rmi building --force', shell=True)
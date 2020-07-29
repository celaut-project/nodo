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
                "Makeit": None,
                "Merkle": None,     # dict
                "Dependency": None, # list
                "Ledger": None, 
                "Tensor": None,
            }):
        super().__init__()
        self.file = file
        self.Id = None
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
            def ordena(dirs):
                print("....ORDENANDO....")
                return dirs
            os.system("mkdir building")
            os.system('sudo docker build -t building registry/for_build/.')
            os.system("docker save building | gzip > building/building.tar.gz")
            os.system("cd building && tar -xvf building.tar.gz")
            dirs = [] # lista de todos los directorios para ordenar.
            for layer in os.listdir("building/"):
                if os.path.isdir("building/"+layer):
                    print("Layer --> ",layer) # Si accedemos directamente, en vez de descomprimir, será bastante mas rapido.
                    for dir in check_output("cd building/"+layer+" && tar -xvf layer.tar", shell=True).decode('utf-8').split("\n")[:-1]:
                        dirs.append(layer+"/"+dir)
            print(dirs)
            output=""
            for adir in ordena(dirs=dirs):
                if adir[-7:]!=".tar.gz":
                    print("Directory --> "+adir[65:])
                    if output=="":output = adir[65:]
                    else: output =  output+"\n"+adir[65:]
                    if adir[-1]!="/":
                        print("Info from --> "+adir)
                        try:
                            info = open("building/"+adir,"r").read().decode('utf-8')
                        except UnicodeDecodeError:
                            info = open("building/"+adir,"br").read()
                        output = output+info
            os.system("rm -rf building")
            os.system("docker rmi building")
            print(output)
            container.update({'Filesys':sha256(output)})
            return container
        container = self.file.get('Container')
        if container == None:
            container = {
                "Entrypoint" : None,    # string
                "Filesys" : None,        # string
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
                if self.file.get('Container').get('Entrypoint') == None:
                    return None
                return {
                    "Id":sha256(self.file.get('Container').get('Entrypoint')),
                    "$ref":"#/Container/Entrypoint"
                }
            def makeLayers():
                def makeLayer(i):
                    def makeBuEl(i):
                        def makeBuild(i):
                            build = self.file.get('Makeit')[i].get('Build')
                            if build is None:
                                return None
                            else:
                                merkle = []
                                for index,b in enumerate(build):
                                    merkle.append({
                                        "Id":sha256(b),
                                        "$ref":"#/Makeit["+str(i)+"]/Build["+str(index)+"]"
                                    })
                                return {
                                    "Id":concat(merkle),
                                    "Merkle": merkle
                                }
                        merkle = [
                            makeBuild(i),
                            {
                                "Id":sha256(self.file.get('Makeit')[i].get('ChainId')),
                                "$ref":"#/Makeit["+str(i)+"]/ChainId"
                            }
                        ]
                        merkle = [make for make in merkle if make != None] # No se concatenan los campos vacios.
                        return {
                            "Id" : concat(merkle),
                            "Merkle": merkle
                        }
                    if i==0: 
                        first = makeBuEl(i)
                        return {
                            "Id":sha256(first.get('Id')),
                            "Merkle":first
                        }
                    else: 
                        merkle = [
                            makeLayer(i-1),
                            makeBuEl(i)
                        ]
                        return {
                            "Id":concat(merkle),
                            "Merkle": merkle
                        }
                return makeLayer(len(self.file.get('Makeit'))-1)
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
                    "Id" : concat(envs),
                    "Merkle": envs
                }
            merkle = [
                makeEntrypoint(),
                makeLayers(),
                makeEnvs()
            ]
            merkle = [make for make in merkle if make != None] # No se concatenan los campos vacios.
            return {
                "Id" : concat(merkle),
                "Merkle": merkle
            }
        merkle = [
            makeContainer()
        ]
        self.file.update({'Merkle' : {'Id':concat(merkle), "Merkle":merkle}})

    def parseMakeit(self):
            Dockerfile = open("registry/for_build/Dockerfile", "r")
            layers_in_file = []
            for l in Dockerfile.readlines():
                command = l.split()[0]
                if command == 'RUN' or command == 'FROM':
                    layers_in_file.append(' '.join(l.split()))
            layers=[]
            for i, l in enumerate(layers_in_file[::-1]):
                build = [l]
                layers.append({'Build':build})
                build = layers[i].get('Build')
            self.file.update({'Makeit' : layers[::-1]})
            Dockerfile.close()

    def calculateId(self):
        self.Id = "Patata.json"

    def save(self):
        print(self.file.get('Merkle'))
        registry = self.registry + self.Id + '.json'
        with open(registry,'w') as file:
            file.write( json.dumps(self.file, indent=4, sort_keys=True) )

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print("\nIMPORTANTE: el Dockerfile no soporta comandos de varias lineas.")
        Hyperfile = Hyper() # Hyperfile
    else:
        print("\n NO HAY QUE USAR PARAMETROS.")

    if os.path.isfile('registry/for_build/Dockerfile') == False or os.path.isfile('registry/for_build/Arch.json') == False:
        print('ForBuild invalido, Dockerfile y Arch.json OBLIGATORIOS ....')
        exit()

    Hyperfile.parseContainer()
    Hyperfile.parseApi()
    Hyperfile.parseDependency()
    ## Demas ..
    Hyperfile.calculateId() # Aqui sha256

    ##Actualizables
    Hyperfile.parseMakeit()
    Hyperfile.makeMerkle()

    Hyperfile.save()
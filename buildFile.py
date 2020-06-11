import sys
import json
from subprocess import run

class Hyper:
    def __init__(self, file={
                "Api": {},
                "Container" : {},
                "Contract": [],
                "Id": "",
                "Import": [],
                "Ledger": "",
                "Tensor": ""
            }):
        super().__init__()
        self.file = file
        self.registry = 'registry/'

    def parseContainer(self):
        def parseInspect(container):
            run('docker inspect building... >> inspect.json')
            inspect = json.load(open('inspect.json','r'))
            inspect.load('')
        def parseDockerfile(container):
            Dockerfile = open("Dockerfile", "r")
            for l in Dockerfile.readlines():
                command = l.split()[0]
                if command == 'RUN' or command == 'FROM':
                    layers = container.get('Layers')
                    # De momento no mira de actualizarla, mete como capa nueva y la id la deja en blanco.
                    layers.append(
                        {
                            "DiffId" : "",
                            "ChainId" : "",
                            "Build" : [' '.join(l.split())] # Si queremos que actualize habria que usar append, de momento no.
                        }
                    )
                    container.update({'Layers' : layers})
                """elif command == 'ENTRYPOINT':
                    entrypoint = container.get('Entrypoint')
                    entrypoint.append( ' '.join(l.split()[1:]) )
                    container.update({'Entrypoint' : entrypoint})
                elif command == 'WORKDIR':
                    container.update({'WorkingDir' : l.split()[1:]})"""
            Dockerfile.close()
            return container
        container = self.file.get('Container')
        if container == {}:
            container = {
                "Envs" : [],
                "Ports" : [],
                "Volumes" : [],
                "WorkingDir" : "",
                "Entrypoint" : [],
                "Layers" : [],
                "OsArch" : []
            }  
        container = parseDockerfile(container)
        container = parseInspect(container)
        self.file.update({'Container' : container})

    def parseApi(self):
        pass

    def makeId(self):
        id = 'xx87tgyhiuji8u97y6tguhjniouy87trfcgvbhnjiouytf'
        self.file.update({'Id':'sha256:'+id})

    def save(self):
        registry = self.registry + self.file.get('Id').split(':')[1] + '.json'
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
            
    run('docker build -t building... .')
    Hyperfile.parseContainer()
    #Hyperfile.parseApi()

    #Hyperfile.makeId()
    Hyperfile.save()
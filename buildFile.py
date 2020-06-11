import sys
import json

class Hyper:
    def __init__(self):
        super().__init__()
        self.file = {
                "Api": {},
                "Container" : {},
                "Contract": [],
                "Id": "",
                "Import": [],
                "Ledger": "",
                "Tensor": ""
            }
        self.registry = 'OOOOO/'

    def parseContainer(self, Dockername):
        #Read Dockerfile
        Dockerfile = open(Dockername, "r")
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
        for l in Dockerfile.readlines():
            command = l.split()[0]
            if command == 'RUN' or command == 'FROM':
                layers = container.get('Layers')
                # De momento no mira de actualizarla, mete como capa nueva y la id la deja en blanco.
                layers.append(
                    {
                        "Id" : "",
                        "Build" : [' '.join(l.split()[1:])] # Si queremos que actualize habria que usar append, de momento no.
                    }
                )
                container.update({'Layers' : layers})
            elif command == 'ENTRYPOINT':
                entrypoint = container.get('Entrypoint')
                entrypoint.append( ' '.join(l.split()[1:]) )
                container.update({'Entrypoint' : entrypoint})
            elif command == 'WORKDIR':
                container.update({'WorkingDir' : l.split()[1:]})
        Dockerfile.close()
        self.file.update({'Container' : container})

    def parseApi(self):
        pass

    def makeId(self):
        self.file.update({'Id':'sha256:x89j3mm4nodl3990lol33n4m3n4m3n443434jjkd21dllfdwidmvlejldkfjh3m4n3kj4b'})

    def save(self):
        registry = self.registry + self.file.get('Id').split(':')[1] + '.json'
        with open(registry,'w') as file:
            file.write( json.dumps(self.file, indent=4, sort_keys=True) )

if __name__ == "__main__":
    Dockerfile = sys.argv[1] # Dockerfile, no soporta comandos de varias lineas.
    Hyperfile = Hyper() # Hyperfile

    Hyperfile.parseContainer(Dockerfile)
    Hyperfile.parseApi()

    Hyperfile.makeId()
    Hyperfile.save()

    # tmb se podria hacer buildFile.py --update Hyperfile.json Dockerfile, para actualizar y añadir valores a uno ya existente.
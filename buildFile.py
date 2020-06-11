import sys
import json

class Hyper:
    def __init__(self):
        super().__init__()
        file = {
                "Api": {},
                "Container" : {},
                "Contract": [],
                "Id": "",
                "Import": [],
                "Ledger": "",
                "Tensor": ""
            }
        registry = 'OOOOO/'

    def parseContainer(self, Dockername):
        #Read Dockerfile
        Dockerfile = open(Dockername, "r")
        container = self.file.get('Container')
        if container == {}:
            container = {
                "Volumes" : [],
                "WorkingDir" : "",
                "Entrypoint" : [],
                "Layers" : [],
            }
        for l in Dockerfile.readlines():
            command = l.split()[0]
            if command == 'RUN' or command == 'FROM':
                layers = container.get('Layers')
                # De momento no mira de actualizarla, mete como capa nueva y la id la deja en blanco.
                layers.append(
                    {
                        "Id" : "",
                        "Build" : [l] # Si queremos que actualize habria que usar append, de momento no.
                    }
                )
                container.update({'Layers' : layers})
            elif command == 'ENTRYPOINT':
                entrypoint = container.get('Entrypoint')
                entrypoint.append(l.split()[1:])
                container.update({'Entrypoint' : entrypoint})
            elif command == 'WORKDIR':
                container.update({'WorkDir' : l.split()[1:]})
        Dockerfile.close()
        self.file.update({'Container' : container})

    def save(self):
        registry = self.registry+self.file.get('Id').split(':')[1]
        with open(registry,'w') as file:
            json.dumps(self.file, file, indent=4, sort_keys=True)


if __name__ == "__main__":
    Hyperfile = Hyper() # Hyperfile
    Dockerfile = sys.argv[1] # Dockerfile, no soporta comandos de varias lineas.
    
    Hyperfile.parseContainer(Dockerfile)
    Hyperfile.parseApi()
    
    Hyperfile.makeId()
    Hyperfile.save()

    # tmb se podria hacer buildFile.py --update Hyperfile.json Dockerfile, para actualizar y añadir valores a uno ya existente.
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

    class Container:
        def __init__(self):
            super().__init__()
            volume = None
            workdir = None
            entrypoint = None
            layers = []
        
    class Layer:
        def __init__(self):
            super().__init__()
            id = None
            build = None

    def parseContainer(self, Dockername):
        #Read Dockerfile
        Dockerfile = open(Dockername, "r")
        container = Container()
        for l in Dockerfile.readlines():
            command = what_is(l.split()[0])
            if command == 'RUN':
                layer = Layer()
                layer.build.append(l)
                container.layers.append(layer)
        Dockerfile.close()

    def save(self):
        json.dumps(self.file, indent=4, sort_keys=True)

if __name__ == "__main__":
    Hyperfile = Hyper() # Hyperfile
    Dockerfile = sys.argv[1] # Dockerfile, no soporta comandos de varias lineas.
    
    Hyperfile.parseContainer(Dockerfile)
    Hyperfile.parseApi()
    
    Hyperfile.makeId()
    Hyperfile.save()

    # tmb se podria hacer buildFile.py --update Hyperfile.json Dockerfile, para actualizar y añadir valores a uno ya existente.
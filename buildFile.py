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
        build = ""
        for l in Dockerfile.readlines():
            build = build+l
        print(build,"\n")
        Dockerfile.close()
        return build

    def save(self):
        json.dumps(self.file, indent=4, sort_keys=True)

if __name__ == "__main__":
    Hyperfile = Hyper() # Hyperfile
    Dockerfile = sys.argv[1] # Dockerfile
    
    Hyperfile.parseContainer(Dockerfile)
    Hyperfile.parseApi()
    
    Hyperfile.makeId()
    Hyperfile.save()
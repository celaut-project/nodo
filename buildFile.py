import sys
import json

def DockerfileToString(Dockername):
    #Read Dockerfile
    Dockerfile = open(Dockername, "r")
    build = ""
    for l in Dockerfile.readlines():
        build = build+l
    print(build,"\n")
    Dockerfile.close()
    return build

def writeBuild(Hypername, string):
    Hyperfile = json.load(open(Hypername,"r"))
    Hyperfile.update({'Container': {'Build':string}})
    return( json.dumps(Hyperfile, indent=4, sort_keys=True) )

if __name__ == "__main__":
    Hyperfile = sys.argv[1]  # Hyperfile
    Dockerfile = sys.argv[2] # Dockerfile
    string = DockerfileToString(Dockerfile)
    json = writeBuild(Hyperfile, string)
    with open(Hyperfile, "w") as json_file:
        json_file.write(json)
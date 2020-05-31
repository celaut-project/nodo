
import json

def makeBuild(Dockername, Hypername):
    
    #Read Dockerfile
    Dockerfile = open(Dockername, "r")
    build = ""
    for l in Dockerfile.readlines():
        build = build+"\\"+l
    print(build,"\n")
    
    #Read Hyperfile
    with open(Hypername) as hf:
        Hyperfile = json.load(hf)
    print(Hyperfile,"\n") 

    #Update Hyperfile.BUILD
    #Hyperfile.update("BUILD":build)

if __name__ == "__main__":
    Dockerfile = "frontier/Dockerfile"
    Hyperfile = "frontier/Hyperfile.json"
    makeBuild(Dockerfile, Hyperfile)